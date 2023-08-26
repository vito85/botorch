#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import warnings
from copy import deepcopy
from itertools import product
from math import pi
from unittest import mock

import torch
from botorch import settings
from botorch.acquisition import (
    LogImprovementMCAcquisitionFunction,
    qLogExpectedImprovement,
    qLogNoisyExpectedImprovement,
)
from botorch.acquisition.input_constructors import ACQF_INPUT_CONSTRUCTOR_REGISTRY
from botorch.acquisition.monte_carlo import (
    qExpectedImprovement,
    qNoisyExpectedImprovement,
)

from botorch.acquisition.objective import (
    ConstrainedMCObjective,
    GenericMCObjective,
    IdentityMCObjective,
    PosteriorTransform,
    ScalarizedPosteriorTransform,
)
from botorch.exceptions import BotorchWarning, UnsupportedError
from botorch.exceptions.errors import BotorchError
from botorch.models import SingleTaskGP
from botorch.sampling.normal import IIDNormalSampler, SobolQMCNormalSampler
from botorch.utils.low_rank import sample_cached_cholesky
from botorch.utils.testing import BotorchTestCase, MockModel, MockPosterior

from botorch.utils.transforms import standardize
from torch import Tensor


def infeasible_con(samples: Tensor) -> Tensor:
    return torch.ones_like(samples[..., 0])


def feasible_con(samples: Tensor) -> Tensor:
    return -torch.ones_like(samples[..., 0])


class DummyLogImprovementAcquisitionFunction(LogImprovementMCAcquisitionFunction):
    def _sample_forward(self, X):
        pass


class DummyNonScalarizingPosteriorTransform(PosteriorTransform):
    scalarize = False

    def evaluate(self, Y):
        pass  # pragma: no cover

    def forward(self, posterior):
        pass  # pragma: no cover


class TestLogImprovementAcquisitionFunction(BotorchTestCase):
    def test_abstract_raises(self):
        with self.assertRaises(TypeError):
            LogImprovementMCAcquisitionFunction()
        # raise if model is multi-output, but no outcome transform or objective
        # are given
        no = "botorch.utils.testing.MockModel.num_outputs"
        with mock.patch(no, new_callable=mock.PropertyMock) as mock_num_outputs:
            mock_num_outputs.return_value = 2
            mm = MockModel(MockPosterior())
            with self.assertRaises(UnsupportedError):
                DummyLogImprovementAcquisitionFunction(model=mm)
        # raise if model is multi-output, but outcome transform does not
        # scalarize and no objetive is given
        with mock.patch(no, new_callable=mock.PropertyMock) as mock_num_outputs:
            mock_num_outputs.return_value = 2
            mm = MockModel(MockPosterior())
            ptf = DummyNonScalarizingPosteriorTransform()
            with self.assertRaises(UnsupportedError):
                DummyLogImprovementAcquisitionFunction(
                    model=mm, posterior_transform=ptf
                )

        mm = MockModel(MockPosterior())
        objective = ConstrainedMCObjective(
            IdentityMCObjective(),
            constraints=[lambda samples: torch.zeros_like(samples[..., 0])],
        )
        with self.assertRaisesRegex(
            BotorchError,
            "Log-Improvement should not be used with `ConstrainedMCObjective`.",
        ):
            DummyLogImprovementAcquisitionFunction(model=mm, objective=objective)


class TestQLogExpectedImprovement(BotorchTestCase):
    def test_q_log_expected_improvement(self):
        self.assertIn(qLogExpectedImprovement, ACQF_INPUT_CONSTRUCTOR_REGISTRY.keys())
        for dtype in (torch.float, torch.double):
            tkwargs = {"device": self.device, "dtype": dtype}
            # the event shape is `b x q x t` = 1 x 1 x 1
            samples = torch.zeros(1, 1, 1, **tkwargs)
            mm = MockModel(MockPosterior(samples=samples))
            # X is `q x d` = 1 x 1. X is a dummy and unused b/c of mocking
            X = torch.zeros(1, 1, **tkwargs)

            # basic test
            sampler = IIDNormalSampler(sample_shape=torch.Size([2]))
            acqf = qExpectedImprovement(model=mm, best_f=0, sampler=sampler)
            log_acqf = qLogExpectedImprovement(model=mm, best_f=0, sampler=sampler)
            self.assertFalse(acqf._fat)  # different default behavior
            self.assertTrue(log_acqf._fat)
            # test initialization
            for k in ["objective", "sampler"]:
                self.assertIn(k, acqf._modules)
                self.assertIn(k, log_acqf._modules)

            res = acqf(X).item()
            self.assertEqual(res, 0.0)
            exp_log_res = log_acqf(X).exp().item()
            # Due to the smooth approximation, the value at zero should be close to, but
            # not exactly zero, and upper-bounded by the tau hyperparameter.
            self.assertTrue(0 < exp_log_res)
            self.assertTrue(exp_log_res <= log_acqf.tau_relu)

            # test shifting best_f value downward to see non-zero improvement
            best_f = -1
            acqf = qExpectedImprovement(model=mm, best_f=best_f, sampler=sampler)
            log_acqf = qLogExpectedImprovement(model=mm, best_f=best_f, sampler=sampler)
            res, exp_log_res = acqf(X), log_acqf(X).exp()
            expected_val = -best_f

            self.assertEqual(res.dtype, dtype)
            self.assertEqual(res.device.type, self.device.type)
            self.assertEqual(res.item(), expected_val)
            # Further away from zero, the value is numerically indistinguishable with
            # single precision arithmetic.
            self.assertEqual(exp_log_res.dtype, dtype)
            self.assertEqual(exp_log_res.device.type, self.device.type)
            self.assertTrue(expected_val <= exp_log_res.item())
            self.assertTrue(exp_log_res.item() <= expected_val + log_acqf.tau_relu)

            # test shifting best_f value upward to see advantage of LogEI
            best_f = 1
            acqf = qExpectedImprovement(model=mm, best_f=best_f, sampler=sampler)
            log_acqf = qLogExpectedImprovement(model=mm, best_f=best_f, sampler=sampler)
            res, log_res = acqf(X), log_acqf(X)
            exp_log_res = log_res.exp()
            expected_val = 0
            self.assertEqual(res.item(), expected_val)
            self.assertTrue(expected_val <= exp_log_res.item())
            self.assertTrue(exp_log_res.item() <= expected_val + log_acqf.tau_relu)
            # However, the log value is large and negative with non-vanishing gradients
            self.assertGreater(-1, log_res.item())
            self.assertGreater(log_res.item(), -100)

            # NOTE: The following tests are adapted from the qEI tests.
            # basic test, no resample
            sampler = IIDNormalSampler(sample_shape=torch.Size([2]), seed=12345)
            acqf = qLogExpectedImprovement(model=mm, best_f=0, sampler=sampler)
            res = acqf(X)
            self.assertTrue(0 < res.exp().item())
            self.assertTrue(res.exp().item() < acqf.tau_relu)
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 1, 1]))
            bs = acqf.sampler.base_samples.clone()
            res = acqf(X)
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))

            # basic test, qmc
            sampler = SobolQMCNormalSampler(sample_shape=torch.Size([2]))
            acqf = qLogExpectedImprovement(model=mm, best_f=0, sampler=sampler)
            res = acqf(X)
            self.assertTrue(0 < res.exp().item())
            self.assertTrue(res.exp().item() < acqf.tau_relu)
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 1, 1]))
            bs = acqf.sampler.base_samples.clone()
            acqf(X)
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))

            # basic test for X_pending and warning
            acqf.set_X_pending()
            self.assertIsNone(acqf.X_pending)
            acqf.set_X_pending(None)
            self.assertIsNone(acqf.X_pending)
            acqf.set_X_pending(X)
            self.assertEqual(acqf.X_pending, X)
            mm._posterior._samples = torch.zeros(1, 2, 1, **tkwargs)
            res = acqf(X)
            X2 = torch.zeros(1, 1, 1, **tkwargs, requires_grad=True)
            with warnings.catch_warnings(record=True) as ws, settings.debug(True):
                acqf.set_X_pending(X2)
                self.assertEqual(acqf.X_pending, X2)
                self.assertEqual(
                    sum(issubclass(w.category, BotorchWarning) for w in ws), 1
                )

            # testing with illegal taus
            with self.assertRaisesRegex(ValueError, "tau_max is not a scalar:"):
                qLogExpectedImprovement(
                    model=mm, best_f=0, tau_max=torch.tensor([1, 2])
                )
            with self.assertRaisesRegex(ValueError, "tau_relu is non-positive:"):
                qLogExpectedImprovement(model=mm, best_f=0, tau_relu=-2)

    def test_q_log_expected_improvement_batch(self):
        for dtype in (torch.float, torch.double):
            # the event shape is `b x q x t` = 2 x 2 x 1
            samples = torch.zeros(2, 2, 1, device=self.device, dtype=dtype)
            samples[0, 0, 0] = 1.0
            mm = MockModel(MockPosterior(samples=samples))

            # X is a dummy and unused b/c of mocking
            X = torch.zeros(2, 2, 1, device=self.device, dtype=dtype)

            # test batch mode
            sampler = IIDNormalSampler(sample_shape=torch.Size([2]))
            acqf = qLogExpectedImprovement(model=mm, best_f=0, sampler=sampler)
            exp_log_res = acqf(X).exp()
            # with no approximations (qEI): self.assertEqual(res[0].item(), 1.0)
            # in the batch case, the values get adjusted toward
            self.assertEqual(exp_log_res.dtype, dtype)
            self.assertEqual(exp_log_res.device.type, self.device.type)
            self.assertTrue(1.0 <= exp_log_res[0].item())
            self.assertTrue(exp_log_res[0].item() <= 1.0 + acqf.tau_relu)
            # self.assertAllClose(exp_log_res[0], torch.ones_like(exp_log_res[0]), )

            # with no approximations (qEI): self.assertEqual(res[1].item(), 0.0)
            self.assertTrue(0 < exp_log_res[1].item())
            self.assertTrue(exp_log_res[1].item() <= acqf.tau_relu)

            # test batch model, batched best_f values
            sampler = IIDNormalSampler(sample_shape=torch.Size([3]))
            acqf = qLogExpectedImprovement(
                model=mm, best_f=torch.Tensor([0, 0]), sampler=sampler
            )
            exp_log_res = acqf(X).exp()
            # with no approximations (qEI): self.assertEqual(res[0].item(), 1.0)
            self.assertTrue(1.0 <= exp_log_res[0].item())
            self.assertTrue(exp_log_res[0].item() <= 1.0 + acqf.tau_relu)
            # with no approximations (qEI): self.assertEqual(res[1].item(), 0.0)
            self.assertTrue(0 < exp_log_res[1].item())
            self.assertTrue(exp_log_res[1].item() <= acqf.tau_relu)

            # test shifting best_f value
            acqf = qLogExpectedImprovement(model=mm, best_f=-1, sampler=sampler)
            exp_log_res = acqf(X).exp()
            # with no approximations (qEI): self.assertEqual(res[0].item(), 2.0)
            # TODO: figure out numerically stable tests and principled tolerances
            # With q > 1, maximum value can get moved down due to L_q-norm approximation
            # of the maximum over the q-batch.
            safe_upper_lower_bound = 1.999
            self.assertTrue(safe_upper_lower_bound <= exp_log_res[0].item())
            self.assertTrue(exp_log_res[0].item() <= 2.0 + acqf.tau_relu + acqf.tau_max)
            # with no approximations (qEI): self.assertEqual(res[1].item(), 1.0)
            self.assertTrue(1.0 <= exp_log_res[1].item())
            # ocurring ~tau_max error when all candidates in a q-batch have the
            # acquisition value
            self.assertTrue(exp_log_res[1].item() <= 1.0 + acqf.tau_relu + acqf.tau_max)

            # test batch mode
            sampler = IIDNormalSampler(sample_shape=torch.Size([2]), seed=12345)
            acqf = qLogExpectedImprovement(model=mm, best_f=0, sampler=sampler)
            # res = acqf(X)  # 1-dim batch
            exp_log_res = acqf(X).exp()  # 1-dim batch
            # with no approximations (qEI): self.assertEqual(res[0].item(), 1.0)
            safe_upper_lower_bound = 0.999
            self.assertTrue(safe_upper_lower_bound <= exp_log_res[0].item())
            self.assertTrue(exp_log_res[0].item() <= 1.0 + acqf.tau_relu)
            # with no approximations (qEI): self.assertEqual(res[1].item(), 0.0)
            self.assertTrue(0.0 <= exp_log_res[1].item())
            self.assertTrue(exp_log_res[1].item() <= 0.0 + acqf.tau_relu)
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 2, 1]))
            bs = acqf.sampler.base_samples.clone()
            acqf(X)
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))
            exp_log_res = acqf(X.expand(2, 2, 1)).exp()  # 2-dim batch
            # self.assertEqual(res[0].item(), 1.0)
            safe_upper_lower_bound = 0.999
            self.assertTrue(safe_upper_lower_bound <= exp_log_res[0].item())
            self.assertTrue(exp_log_res[0].item() <= 1.0 + acqf.tau_relu)
            # self.assertEqual(res[1].item(), 0.0)
            self.assertTrue(0.0 <= exp_log_res[1].item())
            self.assertTrue(exp_log_res[1].item() <= 0.0 + acqf.tau_relu)
            # the base samples should have the batch dim collapsed
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 2, 1]))
            bs = acqf.sampler.base_samples.clone()
            acqf(X.expand(2, 2, 1))
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))

            # test batch mode, qmc
            sampler = SobolQMCNormalSampler(sample_shape=torch.Size([2]))
            acqf = qLogExpectedImprovement(model=mm, best_f=0, sampler=sampler)
            exp_log_res = acqf(X).exp()
            # self.assertEqual(res[0].item(), 1.0)
            safe_upper_lower_bound = 0.999
            self.assertTrue(safe_upper_lower_bound <= exp_log_res[0].item())
            self.assertTrue(exp_log_res[0].item() <= 1.0 + acqf.tau_relu)
            # self.assertEqual(res[1].item(), 0.0)
            self.assertTrue(0.0 <= exp_log_res[1].item())
            self.assertTrue(exp_log_res[1].item() <= 0.0 + acqf.tau_relu)
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 2, 1]))
            bs = acqf.sampler.base_samples.clone()
            acqf(X)
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))

    # # TODO: Test different objectives (incl. constraints)


class TestQLogNoisyExpectedImprovement(BotorchTestCase):
    def test_q_log_noisy_expected_improvement(self):
        self.assertIn(
            qLogNoisyExpectedImprovement, ACQF_INPUT_CONSTRUCTOR_REGISTRY.keys()
        )
        for dtype in (torch.float, torch.double):
            # the event shape is `b x q x t` = 1 x 2 x 1
            samples_noisy = torch.tensor([0.0, 1.0], device=self.device, dtype=dtype)
            samples_noisy = samples_noisy.view(1, 2, 1)
            # X_baseline is `q' x d` = 1 x 1
            X_baseline = torch.zeros(1, 1, device=self.device, dtype=dtype)
            mm_noisy = MockModel(MockPosterior(samples=samples_noisy))
            # X is `q x d` = 1 x 1
            X = torch.zeros(1, 1, device=self.device, dtype=dtype)

            # basic test
            sampler = IIDNormalSampler(sample_shape=torch.Size([2]))
            kwargs = {
                "model": mm_noisy,
                "X_baseline": X_baseline,
                "sampler": sampler,
                "prune_baseline": False,
                "cache_root": False,
            }
            acqf = qNoisyExpectedImprovement(**kwargs)
            log_acqf = qLogNoisyExpectedImprovement(**kwargs)

            res = acqf(X)
            self.assertEqual(res.item(), 1.0)
            log_res = log_acqf(X)
            self.assertEqual(log_res.dtype, dtype)
            self.assertEqual(log_res.device.type, self.device.type)
            self.assertAllClose(log_res.exp().item(), 1.0)

            # basic test
            sampler = IIDNormalSampler(sample_shape=torch.Size([2]), seed=12345)
            kwargs = {
                "model": mm_noisy,
                "X_baseline": X_baseline,
                "sampler": sampler,
                "prune_baseline": False,
                "cache_root": False,
            }
            log_acqf = qLogNoisyExpectedImprovement(**kwargs)
            log_res = log_acqf(X)
            self.assertEqual(log_res.exp().item(), 1.0)
            self.assertEqual(
                log_acqf.sampler.base_samples.shape, torch.Size([2, 1, 2, 1])
            )
            bs = log_acqf.sampler.base_samples.clone()
            log_acqf(X)
            self.assertTrue(torch.equal(log_acqf.sampler.base_samples, bs))

            # basic test, qmc
            sampler = SobolQMCNormalSampler(sample_shape=torch.Size([2]))
            kwargs = {
                "model": mm_noisy,
                "X_baseline": X_baseline,
                "sampler": sampler,
                "prune_baseline": False,
                "cache_root": False,
            }
            log_acqf = qLogNoisyExpectedImprovement(**kwargs)
            log_res = log_acqf(X)
            self.assertEqual(log_res.exp().item(), 1.0)
            self.assertEqual(
                log_acqf.sampler.base_samples.shape, torch.Size([2, 1, 2, 1])
            )
            bs = log_acqf.sampler.base_samples.clone()
            log_acqf(X)
            self.assertTrue(torch.equal(log_acqf.sampler.base_samples, bs))

            # basic test for X_pending and warning
            sampler = SobolQMCNormalSampler(sample_shape=torch.Size([2]))
            samples_noisy_pending = torch.tensor(
                [1.0, 0.0, 0.0], device=self.device, dtype=dtype
            )
            samples_noisy_pending = samples_noisy_pending.view(1, 3, 1)
            mm_noisy_pending = MockModel(MockPosterior(samples=samples_noisy_pending))
            kwargs = {
                "model": mm_noisy_pending,
                "X_baseline": X_baseline,
                "sampler": sampler,
                "prune_baseline": False,
                "cache_root": False,
            }
            # copy for log version
            log_acqf = qLogNoisyExpectedImprovement(**kwargs)
            log_acqf.set_X_pending()
            self.assertIsNone(log_acqf.X_pending)
            log_acqf.set_X_pending(None)
            self.assertIsNone(log_acqf.X_pending)
            log_acqf.set_X_pending(X)
            self.assertEqual(log_acqf.X_pending, X)
            log_acqf(X)
            X2 = torch.zeros(
                1, 1, 1, device=self.device, dtype=dtype, requires_grad=True
            )
            with warnings.catch_warnings(record=True) as ws, settings.debug(True):
                log_acqf.set_X_pending(X2)
                self.assertEqual(log_acqf.X_pending, X2)
                self.assertEqual(
                    sum(issubclass(w.category, BotorchWarning) for w in ws), 1
                )

    def test_q_noisy_expected_improvement_batch(self):
        for dtype in (torch.float, torch.double):
            # the event shape is `b x q x t` = 2 x 3 x 1
            samples_noisy = torch.zeros(2, 3, 1, device=self.device, dtype=dtype)
            samples_noisy[0, -1, 0] = 1.0
            mm_noisy = MockModel(MockPosterior(samples=samples_noisy))
            # X is `q x d` = 1 x 1
            X = torch.zeros(2, 2, 1, device=self.device, dtype=dtype)
            X_baseline = torch.zeros(1, 1, device=self.device, dtype=dtype)

            # test batch mode
            sampler = IIDNormalSampler(sample_shape=torch.Size([2]))
            kwargs = {
                "model": mm_noisy,
                "X_baseline": X_baseline,
                "sampler": sampler,
                "prune_baseline": False,
                "cache_root": False,
            }
            acqf = qLogNoisyExpectedImprovement(**kwargs)
            res = acqf(X).exp()
            expected_res = torch.tensor([1.0, 0.0], dtype=dtype, device=self.device)
            self.assertAllClose(res, expected_res, atol=acqf.tau_relu)
            self.assertGreater(res[1].item(), 0.0)
            self.assertGreater(acqf.tau_relu, res[1].item())

            # test batch mode
            sampler = IIDNormalSampler(sample_shape=torch.Size([2]), seed=12345)
            acqf = qLogNoisyExpectedImprovement(
                model=mm_noisy,
                X_baseline=X_baseline,
                sampler=sampler,
                prune_baseline=False,
                cache_root=False,
            )
            res = acqf(X).exp()  # 1-dim batch
            expected_res = torch.tensor([1.0, 0.0], dtype=dtype, device=self.device)
            self.assertAllClose(res, expected_res, atol=acqf.tau_relu)
            self.assertGreater(res[1].item(), 0.0)
            self.assertGreater(acqf.tau_relu, res[1].item())
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 3, 1]))
            bs = acqf.sampler.base_samples.clone()
            acqf(X)
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))
            res = acqf(X.expand(2, 2, 1)).exp()  # 2-dim batch
            expected_res = torch.tensor([1.0, 0.0], dtype=dtype, device=self.device)
            self.assertAllClose(res, expected_res, atol=acqf.tau_relu)
            self.assertGreater(res[1].item(), 0.0)
            self.assertGreater(acqf.tau_relu, res[1].item())
            # the base samples should have the batch dim collapsed
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 3, 1]))
            bs = acqf.sampler.base_samples.clone()
            acqf(X.expand(2, 2, 1))
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))

            # test batch mode, qmc
            sampler = SobolQMCNormalSampler(sample_shape=torch.Size([2]))
            acqf = qLogNoisyExpectedImprovement(
                model=mm_noisy,
                X_baseline=X_baseline,
                sampler=sampler,
                prune_baseline=False,
                cache_root=False,
            )
            res = acqf(X).exp()
            expected_res = torch.tensor([1.0, 0.0], dtype=dtype, device=self.device)
            self.assertAllClose(res, expected_res, atol=acqf.tau_relu)
            self.assertGreater(res[1].item(), 0.0)
            self.assertGreater(acqf.tau_relu, res[1].item())
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 3, 1]))
            bs = acqf.sampler.base_samples.clone()
            acqf(X)
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))

    def test_prune_baseline(self):
        no = "botorch.utils.testing.MockModel.num_outputs"
        prune = "botorch.acquisition.logei.prune_inferior_points"
        for dtype in (torch.float, torch.double):
            X_baseline = torch.zeros(1, 1, device=self.device, dtype=dtype)
            X_pruned = torch.rand(1, 1, device=self.device, dtype=dtype)
            with mock.patch(no, new_callable=mock.PropertyMock) as mock_num_outputs:
                mock_num_outputs.return_value = 1
                mm = MockModel(MockPosterior(samples=X_baseline))
                with mock.patch(prune, return_value=X_pruned) as mock_prune:
                    acqf = qLogNoisyExpectedImprovement(
                        model=mm,
                        X_baseline=X_baseline,
                        prune_baseline=True,
                        cache_root=False,
                    )
                mock_prune.assert_called_once()
                self.assertTrue(torch.equal(acqf.X_baseline, X_pruned))
                with mock.patch(prune, return_value=X_pruned) as mock_prune:
                    acqf = qLogNoisyExpectedImprovement(
                        model=mm,
                        X_baseline=X_baseline,
                        prune_baseline=True,
                        marginalize_dim=-3,
                        cache_root=False,
                    )
                    _, kwargs = mock_prune.call_args
                    self.assertEqual(kwargs["marginalize_dim"], -3)

    def test_cache_root(self):
        sample_cached_path = (
            "botorch.acquisition.cached_cholesky.sample_cached_cholesky"
        )
        raw_state_dict = {
            "likelihood.noise_covar.raw_noise": torch.tensor(
                [[0.0895], [0.2594]], dtype=torch.float64
            ),
            "mean_module.raw_constant": torch.tensor(
                [-0.4545, -0.1285], dtype=torch.float64
            ),
            "covar_module.raw_outputscale": torch.tensor(
                [1.4876, 1.4897], dtype=torch.float64
            ),
            "covar_module.base_kernel.raw_lengthscale": torch.tensor(
                [[[-0.7202, -0.2868]], [[-0.8794, -1.2877]]], dtype=torch.float64
            ),
        }
        # test batched models (e.g. for MCMC)
        for train_batch_shape, m, dtype in product(
            (torch.Size([]), torch.Size([3])), (1, 2), (torch.float, torch.double)
        ):
            state_dict = deepcopy(raw_state_dict)
            for k, v in state_dict.items():
                if m == 1:
                    v = v[0]
                if len(train_batch_shape) > 0:
                    v = v.unsqueeze(0).expand(*train_batch_shape, *v.shape)
                state_dict[k] = v
            tkwargs = {"device": self.device, "dtype": dtype}
            if m == 2:
                objective = GenericMCObjective(lambda Y, X: Y.sum(dim=-1))
            else:
                objective = None
            for k, v in state_dict.items():
                state_dict[k] = v.to(**tkwargs)
            all_close_kwargs = (
                {
                    "atol": 1e-1,
                    "rtol": 0.0,
                }
                if dtype == torch.float
                else {"atol": 1e-4, "rtol": 0.0}
            )
            torch.manual_seed(1234)
            train_X = torch.rand(*train_batch_shape, 3, 2, **tkwargs)
            train_Y = (
                torch.sin(train_X * 2 * pi)
                + torch.randn(*train_batch_shape, 3, 2, **tkwargs)
            )[..., :m]
            train_Y = standardize(train_Y)
            model = SingleTaskGP(
                train_X,
                train_Y,
            )
            if len(train_batch_shape) > 0:
                X_baseline = train_X[0]
            else:
                X_baseline = train_X
            model.load_state_dict(state_dict, strict=False)
            sampler = IIDNormalSampler(sample_shape=torch.Size([5]), seed=0)
            torch.manual_seed(0)
            acqf = qLogNoisyExpectedImprovement(
                model=model,
                X_baseline=X_baseline,
                sampler=sampler,
                objective=objective,
                prune_baseline=False,
                cache_root=True,
            )

            orig_base_samples = acqf.base_sampler.base_samples.detach().clone()
            sampler2 = IIDNormalSampler(sample_shape=torch.Size([5]), seed=0)
            sampler2.base_samples = orig_base_samples
            torch.manual_seed(0)
            acqf_no_cache = qLogNoisyExpectedImprovement(
                model=model,
                X_baseline=X_baseline,
                sampler=sampler2,
                objective=objective,
                prune_baseline=False,
                cache_root=False,
            )
            for q, batch_shape in product(
                (1, 3), (torch.Size([]), torch.Size([3]), torch.Size([4, 3]))
            ):
                acqf.q_in = -1
                acqf_no_cache.q_in = -1
                test_X = (
                    0.3 + 0.05 * torch.randn(*batch_shape, q, 2, **tkwargs)
                ).requires_grad_(True)
                with mock.patch(
                    sample_cached_path, wraps=sample_cached_cholesky
                ) as mock_sample_cached:
                    torch.manual_seed(0)
                    val = acqf(test_X).exp()
                    mock_sample_cached.assert_called_once()
                val.sum().backward()
                base_samples = acqf.sampler.base_samples.detach().clone()
                X_grad = test_X.grad.clone()
                test_X2 = test_X.detach().clone().requires_grad_(True)
                acqf_no_cache.sampler.base_samples = base_samples
                with mock.patch(
                    sample_cached_path, wraps=sample_cached_cholesky
                ) as mock_sample_cached:
                    torch.manual_seed(0)
                    val2 = acqf_no_cache(test_X2).exp()
                    mock_sample_cached.assert_not_called()
                self.assertAllClose(val, val2, **all_close_kwargs)
                val2.sum().backward()
                self.assertAllClose(X_grad, test_X2.grad, **all_close_kwargs)
            # test we fall back to standard sampling for
            # ill-conditioned covariances
            acqf._baseline_L = torch.zeros_like(acqf._baseline_L)
            with warnings.catch_warnings(record=True) as ws, settings.debug(True):
                with torch.no_grad():
                    acqf(test_X)
            self.assertEqual(sum(issubclass(w.category, BotorchWarning) for w in ws), 1)

        # test w/ posterior transform
        X_baseline = torch.rand(2, 1)
        model = SingleTaskGP(X_baseline, torch.randn(2, 1))
        pt = ScalarizedPosteriorTransform(weights=torch.tensor([-1]))
        with mock.patch.object(
            qLogNoisyExpectedImprovement,
            "_compute_root_decomposition",
        ) as mock_cache_root:
            acqf = qLogNoisyExpectedImprovement(
                model=model,
                X_baseline=X_baseline,
                sampler=IIDNormalSampler(sample_shape=torch.Size([1])),
                posterior_transform=pt,
                prune_baseline=False,
                cache_root=True,
            )
            tf_post = model.posterior(X_baseline, posterior_transform=pt)
            self.assertTrue(
                torch.allclose(
                    tf_post.mean, mock_cache_root.call_args[-1]["posterior"].mean
                )
            )

        # testing constraints
        n, d, m = 8, 1, 3
        X_baseline = torch.rand(n, d)
        model = SingleTaskGP(X_baseline, torch.randn(n, m))  # batched model
        nei_args = {
            "model": model,
            "X_baseline": X_baseline,
            "prune_baseline": False,
            "cache_root": True,
            "posterior_transform": ScalarizedPosteriorTransform(weights=torch.ones(m)),
            "sampler": SobolQMCNormalSampler(torch.Size([5])),
        }
        acqf = qLogNoisyExpectedImprovement(**nei_args)
        X = torch.randn_like(X_baseline)
        for con in [feasible_con, infeasible_con]:
            with self.subTest(con=con):
                target = "botorch.acquisition.utils.get_infeasible_cost"
                infcost = torch.tensor([3], device=self.device, dtype=dtype)
                with mock.patch(target, return_value=infcost):
                    cacqf = qLogNoisyExpectedImprovement(**nei_args, constraints=[con])

                _, obj = cacqf._get_samples_and_objectives(X)
                best_feas_f = cacqf.compute_best_f(obj)
                if con is feasible_con:
                    self.assertAllClose(best_feas_f, acqf.compute_best_f(obj))
                else:
                    self.assertAllClose(
                        best_feas_f, torch.full_like(obj[..., [0]], -infcost.item())
                    )
        # TODO: Test different objectives (incl. constraints)
