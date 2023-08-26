#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import warnings
from copy import deepcopy
from functools import partial
from itertools import product
from math import pi
from unittest import mock

import torch
from botorch import settings
from botorch.acquisition.monte_carlo import (
    MCAcquisitionFunction,
    qExpectedImprovement,
    qNoisyExpectedImprovement,
    qProbabilityOfImprovement,
    qSimpleRegret,
    qUpperConfidenceBound,
    SampleReducingMCAcquisitionFunction,
)
from botorch.acquisition.objective import (
    ConstrainedMCObjective,
    GenericMCObjective,
    IdentityMCObjective,
    PosteriorTransform,
    ScalarizedPosteriorTransform,
)
from botorch.exceptions import BotorchWarning, UnsupportedError
from botorch.models import SingleTaskGP
from botorch.sampling.normal import IIDNormalSampler, SobolQMCNormalSampler
from botorch.utils.low_rank import sample_cached_cholesky
from botorch.utils.testing import BotorchTestCase, MockModel, MockPosterior
from botorch.utils.transforms import standardize
from torch import Tensor


class DummyMCAcquisitionFunction(MCAcquisitionFunction):
    def forward(self, X):
        pass


class DummyReducingMCAcquisitionFunction(SampleReducingMCAcquisitionFunction):
    def _sample_forward(self, X):
        pass


class DummyNonScalarizingPosteriorTransform(PosteriorTransform):
    scalarize = False

    def evaluate(self, Y):
        pass  # pragma: no cover

    def forward(self, posterior):
        pass  # pragma: no cover


def infeasible_con(samples: Tensor) -> Tensor:
    return torch.ones_like(samples[..., 0])


def feasible_con(samples: Tensor) -> Tensor:
    return -torch.ones_like(samples[..., 0])


class TestMCAcquisitionFunction(BotorchTestCase):
    def test_abstract_raises(self):
        for acqf_class in (MCAcquisitionFunction, SampleReducingMCAcquisitionFunction):
            with self.assertRaises(TypeError):
                acqf_class()

        # raise if model is multi-output, but no outcome transform or objective
        # are given
        no = "botorch.utils.testing.MockModel.num_outputs"
        with mock.patch(no, new_callable=mock.PropertyMock) as mock_num_outputs:
            mock_num_outputs.return_value = 2
            mm = MockModel(MockPosterior())
            for dummy in (
                DummyMCAcquisitionFunction,
                DummyReducingMCAcquisitionFunction,
            ):
                with self.assertRaises(UnsupportedError):
                    dummy(model=mm)
                # raise if model is multi-output, but outcome transform does not
                # scalarize and no objetive is given
                with mock.patch(no, new_callable=mock.PropertyMock) as mock_num_outputs:
                    mock_num_outputs.return_value = 2
                    mm = MockModel(MockPosterior())
                    ptf = DummyNonScalarizingPosteriorTransform()
                    with self.assertRaises(UnsupportedError):
                        dummy(model=mm, posterior_transform=ptf)


class TestQExpectedImprovement(BotorchTestCase):
    def test_q_expected_improvement(self):
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
            # test initialization
            for k in ["objective", "sampler"]:
                self.assertIn(k, acqf._modules)

            res = acqf(X)
            self.assertEqual(res.item(), 0.0)

            # test shifting best_f value
            acqf = qExpectedImprovement(model=mm, best_f=-1, sampler=sampler)
            res = acqf(X)
            self.assertEqual(res.item(), 1.0)

            # TODO: Test batched best_f, batched model, batched evaluation

            # basic test, no resample
            sampler = IIDNormalSampler(sample_shape=torch.Size([2]), seed=12345)
            acqf = qExpectedImprovement(model=mm, best_f=0, sampler=sampler)
            res = acqf(X)
            self.assertEqual(res.item(), 0.0)
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 1, 1]))
            bs = acqf.sampler.base_samples.clone()
            res = acqf(X)
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))

            # basic test, qmc
            sampler = SobolQMCNormalSampler(sample_shape=torch.Size([2]))
            acqf = qExpectedImprovement(model=mm, best_f=0, sampler=sampler)
            res = acqf(X)
            self.assertEqual(res.item(), 0.0)
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

    def test_q_expected_improvement_batch(self):
        for dtype in (torch.float, torch.double):
            # the event shape is `b x q x t` = 2 x 2 x 1
            samples = torch.zeros(2, 2, 1, device=self.device, dtype=dtype)
            samples[0, 0, 0] = 1.0
            mm = MockModel(MockPosterior(samples=samples))

            # X is a dummy and unused b/c of mocking
            X = torch.zeros(2, 2, 1, device=self.device, dtype=dtype)

            # test batch mode
            sampler = IIDNormalSampler(sample_shape=torch.Size([2]))
            acqf = qExpectedImprovement(model=mm, best_f=0, sampler=sampler)
            res = acqf(X)
            self.assertEqual(res[0].item(), 1.0)
            self.assertEqual(res[1].item(), 0.0)

            # test batch model, batched best_f values
            sampler = IIDNormalSampler(sample_shape=torch.Size([3]))
            acqf = qExpectedImprovement(
                model=mm, best_f=torch.Tensor([0, 0]), sampler=sampler
            )
            res = acqf(X)
            self.assertEqual(res[0].item(), 1.0)
            self.assertEqual(res[1].item(), 0.0)

            # test shifting best_f value
            acqf = qExpectedImprovement(model=mm, best_f=-1, sampler=sampler)
            res = acqf(X)
            self.assertEqual(res[0].item(), 2.0)
            self.assertEqual(res[1].item(), 1.0)

            # test batch mode
            sampler = IIDNormalSampler(sample_shape=torch.Size([2]), seed=12345)
            acqf = qExpectedImprovement(model=mm, best_f=0, sampler=sampler)
            res = acqf(X)  # 1-dim batch
            self.assertEqual(res[0].item(), 1.0)
            self.assertEqual(res[1].item(), 0.0)
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 2, 1]))
            bs = acqf.sampler.base_samples.clone()
            acqf(X)
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))
            res = acqf(X.expand(2, 2, 1))  # 2-dim batch
            self.assertEqual(res[0].item(), 1.0)
            self.assertEqual(res[1].item(), 0.0)
            # the base samples should have the batch dim collapsed
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 2, 1]))
            bs = acqf.sampler.base_samples.clone()
            acqf(X.expand(2, 2, 1))
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))

            # test batch mode, qmc
            sampler = SobolQMCNormalSampler(sample_shape=torch.Size([2]))
            acqf = qExpectedImprovement(model=mm, best_f=0, sampler=sampler)
            res = acqf(X)
            self.assertEqual(res[0].item(), 1.0)
            self.assertEqual(res[1].item(), 0.0)
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 2, 1]))
            bs = acqf.sampler.base_samples.clone()
            acqf(X)
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))

    # TODO: Test different objectives (incl. constraints)


class TestQNoisyExpectedImprovement(BotorchTestCase):
    def test_q_noisy_expected_improvement(self):
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
            acqf = qNoisyExpectedImprovement(
                model=mm_noisy,
                X_baseline=X_baseline,
                sampler=sampler,
                prune_baseline=False,
                cache_root=False,
            )
            res = acqf(X)
            self.assertEqual(res.item(), 1.0)

            # basic test
            sampler = IIDNormalSampler(sample_shape=torch.Size([2]), seed=12345)
            acqf = qNoisyExpectedImprovement(
                model=mm_noisy,
                X_baseline=X_baseline,
                sampler=sampler,
                prune_baseline=False,
                cache_root=False,
            )
            res = acqf(X)
            self.assertEqual(res.item(), 1.0)
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 2, 1]))
            bs = acqf.sampler.base_samples.clone()
            acqf(X)
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))

            # basic test, qmc
            sampler = SobolQMCNormalSampler(sample_shape=torch.Size([2]))
            acqf = qNoisyExpectedImprovement(
                model=mm_noisy,
                X_baseline=X_baseline,
                sampler=sampler,
                prune_baseline=False,
                cache_root=False,
            )
            res = acqf(X)
            self.assertEqual(res.item(), 1.0)
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 2, 1]))
            bs = acqf.sampler.base_samples.clone()
            acqf(X)
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))

            # basic test for X_pending and warning
            sampler = SobolQMCNormalSampler(sample_shape=torch.Size([2]))
            samples_noisy_pending = torch.tensor(
                [1.0, 0.0, 0.0], device=self.device, dtype=dtype
            )
            samples_noisy_pending = samples_noisy_pending.view(1, 3, 1)
            mm_noisy_pending = MockModel(MockPosterior(samples=samples_noisy_pending))
            acqf = qNoisyExpectedImprovement(
                model=mm_noisy_pending,
                X_baseline=X_baseline,
                sampler=sampler,
                prune_baseline=False,
                cache_root=False,
            )
            acqf.set_X_pending()
            self.assertIsNone(acqf.X_pending)
            acqf.set_X_pending(None)
            self.assertIsNone(acqf.X_pending)
            acqf.set_X_pending(X)
            self.assertEqual(acqf.X_pending, X)
            res = acqf(X)
            X2 = torch.zeros(
                1, 1, 1, device=self.device, dtype=dtype, requires_grad=True
            )
            with warnings.catch_warnings(record=True) as ws, settings.debug(True):
                acqf.set_X_pending(X2)
                self.assertEqual(acqf.X_pending, X2)
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
            acqf = qNoisyExpectedImprovement(
                model=mm_noisy,
                X_baseline=X_baseline,
                sampler=sampler,
                prune_baseline=False,
                cache_root=False,
            )
            res = acqf(X)
            self.assertEqual(res[0].item(), 1.0)
            self.assertEqual(res[1].item(), 0.0)

            # test batch mode
            sampler = IIDNormalSampler(sample_shape=torch.Size([2]), seed=12345)
            acqf = qNoisyExpectedImprovement(
                model=mm_noisy,
                X_baseline=X_baseline,
                sampler=sampler,
                prune_baseline=False,
                cache_root=False,
            )
            res = acqf(X)  # 1-dim batch
            self.assertEqual(res[0].item(), 1.0)
            self.assertEqual(res[1].item(), 0.0)
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 3, 1]))
            bs = acqf.sampler.base_samples.clone()
            acqf(X)
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))
            res = acqf(X.expand(2, 2, 1))  # 2-dim batch
            self.assertEqual(res[0].item(), 1.0)
            self.assertEqual(res[1].item(), 0.0)
            # the base samples should have the batch dim collapsed
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 3, 1]))
            bs = acqf.sampler.base_samples.clone()
            acqf(X.expand(2, 2, 1))
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))

            # test batch mode, qmc
            sampler = SobolQMCNormalSampler(sample_shape=torch.Size([2]))
            acqf = qNoisyExpectedImprovement(
                model=mm_noisy,
                X_baseline=X_baseline,
                sampler=sampler,
                prune_baseline=False,
                cache_root=False,
            )
            res = acqf(X)
            self.assertEqual(res[0].item(), 1.0)
            self.assertEqual(res[1].item(), 0.0)
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 3, 1]))
            bs = acqf.sampler.base_samples.clone()
            acqf(X)
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))

    def test_prune_baseline(self):
        no = "botorch.utils.testing.MockModel.num_outputs"
        prune = "botorch.acquisition.monte_carlo.prune_inferior_points"
        for dtype in (torch.float, torch.double):
            X_baseline = torch.zeros(1, 1, device=self.device, dtype=dtype)
            X_pruned = torch.rand(1, 1, device=self.device, dtype=dtype)
            with mock.patch(no, new_callable=mock.PropertyMock) as mock_num_outputs:
                mock_num_outputs.return_value = 1
                mm = MockModel(MockPosterior(samples=X_baseline))
                with mock.patch(prune, return_value=X_pruned) as mock_prune:
                    acqf = qNoisyExpectedImprovement(
                        model=mm,
                        X_baseline=X_baseline,
                        prune_baseline=True,
                        cache_root=False,
                    )
                mock_prune.assert_called_once()
                self.assertTrue(torch.equal(acqf.X_baseline, X_pruned))
                with mock.patch(prune, return_value=X_pruned) as mock_prune:
                    acqf = qNoisyExpectedImprovement(
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
            acqf = qNoisyExpectedImprovement(
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
            acqf_no_cache = qNoisyExpectedImprovement(
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
                    val = acqf(test_X)
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
                    val2 = acqf_no_cache(test_X2)
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
            qNoisyExpectedImprovement,
            "_compute_root_decomposition",
        ) as mock_cache_root:
            acqf = qNoisyExpectedImprovement(
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
            "sampler": SobolQMCNormalSampler(sample_shape=torch.Size([5])),
        }
        acqf = qNoisyExpectedImprovement(**nei_args)
        X = torch.randn_like(X_baseline)
        for con in [feasible_con, infeasible_con]:
            with self.subTest(con=con):
                target = "botorch.acquisition.utils.get_infeasible_cost"
                infcost = torch.tensor([3], device=self.device, dtype=dtype)
                with mock.patch(target, return_value=infcost):
                    cacqf = qNoisyExpectedImprovement(**nei_args, constraints=[con])

                _, obj = cacqf._get_samples_and_objectives(X)
                best_feas_f = cacqf.compute_best_f(obj)
                if con is feasible_con:
                    self.assertAllClose(best_feas_f, acqf.compute_best_f(obj))
                else:
                    self.assertAllClose(
                        best_feas_f, torch.full_like(obj[..., [0]], -infcost.item())
                    )
        # TODO: Test different objectives (incl. constraints)


class TestQProbabilityOfImprovement(BotorchTestCase):
    def test_q_probability_of_improvement(self):
        for dtype in (torch.float, torch.double):
            # the event shape is `b x q x t` = 1 x 1 x 1
            samples = torch.zeros(1, 1, 1, device=self.device, dtype=dtype)
            mm = MockModel(MockPosterior(samples=samples))
            # X is `q x d` = 1 x 1. X is a dummy and unused b/c of mocking
            X = torch.zeros(1, 1, device=self.device, dtype=dtype)

            # basic test
            sampler = IIDNormalSampler(sample_shape=torch.Size([2]))
            acqf = qProbabilityOfImprovement(model=mm, best_f=0, sampler=sampler)
            res = acqf(X)
            self.assertEqual(res.item(), 0.5)

            # basic test
            sampler = IIDNormalSampler(sample_shape=torch.Size([2]), seed=12345)
            acqf = qProbabilityOfImprovement(model=mm, best_f=0, sampler=sampler)
            res = acqf(X)
            self.assertEqual(res.item(), 0.5)
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 1, 1]))
            bs = acqf.sampler.base_samples.clone()
            res = acqf(X)
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))

            # basic test, qmc
            sampler = SobolQMCNormalSampler(sample_shape=torch.Size([2]))
            acqf = qProbabilityOfImprovement(model=mm, best_f=0, sampler=sampler)
            res = acqf(X)
            self.assertEqual(res.item(), 0.5)
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
            mm._posterior._samples = mm._posterior._samples.expand(-1, 2, -1)
            res = acqf(X)
            X2 = torch.zeros(
                1, 1, 1, device=self.device, dtype=dtype, requires_grad=True
            )
            with warnings.catch_warnings(record=True) as ws, settings.debug(True):
                acqf.set_X_pending(X2)
                self.assertEqual(acqf.X_pending, X2)
                self.assertEqual(
                    sum(issubclass(w.category, BotorchWarning) for w in ws), 1
                )

    def test_q_probability_of_improvement_batch(self):
        # the event shape is `b x q x t` = 2 x 2 x 1
        for dtype in (torch.float, torch.double):
            samples = torch.zeros(2, 2, 1, device=self.device, dtype=dtype)
            samples[0, 0, 0] = 1.0
            mm = MockModel(MockPosterior(samples=samples))

            # X is a dummy and unused b/c of mocking
            X = torch.zeros(2, 2, 1, device=self.device, dtype=dtype)

            # test batch mode
            sampler = IIDNormalSampler(sample_shape=torch.Size([2]))
            acqf = qProbabilityOfImprovement(model=mm, best_f=0, sampler=sampler)
            res = acqf(X)
            self.assertEqual(res[0].item(), 1.0)
            self.assertEqual(res[1].item(), 0.5)

            # test batch model, batched best_f values
            sampler = IIDNormalSampler(sample_shape=torch.Size([3]))
            acqf = qProbabilityOfImprovement(
                model=mm, best_f=torch.Tensor([0, 0]), sampler=sampler
            )
            res = acqf(X)
            self.assertEqual(res[0].item(), 1.0)
            self.assertEqual(res[1].item(), 0.5)

            # test batch mode
            sampler = IIDNormalSampler(sample_shape=torch.Size([2]), seed=12345)
            acqf = qProbabilityOfImprovement(model=mm, best_f=0, sampler=sampler)
            res = acqf(X)  # 1-dim batch
            self.assertEqual(res[0].item(), 1.0)
            self.assertEqual(res[1].item(), 0.5)
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 2, 1]))
            bs = acqf.sampler.base_samples.clone()
            acqf(X)
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))
            res = acqf(X.expand(2, -1, 1))  # 2-dim batch
            self.assertEqual(res[0].item(), 1.0)
            self.assertEqual(res[1].item(), 0.5)
            # the base samples should have the batch dim collapsed
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 2, 1]))
            bs = acqf.sampler.base_samples.clone()
            acqf(X.expand(2, -1, 1))
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))

            # test batch mode, qmc
            sampler = SobolQMCNormalSampler(sample_shape=torch.Size([2]))
            acqf = qProbabilityOfImprovement(model=mm, best_f=0, sampler=sampler)
            res = acqf(X)
            self.assertEqual(res[0].item(), 1.0)
            self.assertEqual(res[1].item(), 0.5)
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 2, 1]))
            bs = acqf.sampler.base_samples.clone()
            acqf(X)
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))

    # TODO: Test different objectives (incl. constraints)


class TestQSimpleRegret(BotorchTestCase):
    def test_q_simple_regret(self):
        for dtype in (torch.float, torch.double):
            # the event shape is `b x q x t` = 1 x 1 x 1
            samples = torch.zeros(1, 1, 1, device=self.device, dtype=dtype)
            mm = MockModel(MockPosterior(samples=samples))
            # X is `q x d` = 1 x 1. X is a dummy and unused b/c of mocking
            X = torch.zeros(1, 1, device=self.device, dtype=dtype)

            # basic test
            sampler = IIDNormalSampler(sample_shape=torch.Size([2]))
            acqf = qSimpleRegret(model=mm, sampler=sampler)
            res = acqf(X)
            self.assertEqual(res.item(), 0.0)

            # basic test
            sampler = IIDNormalSampler(sample_shape=torch.Size([2]), seed=12345)
            acqf = qSimpleRegret(model=mm, sampler=sampler)
            res = acqf(X)
            self.assertEqual(res.item(), 0.0)
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 1, 1]))
            bs = acqf.sampler.base_samples.clone()
            res = acqf(X)
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))

            # basic test, qmc
            sampler = SobolQMCNormalSampler(sample_shape=torch.Size([2]))
            acqf = qSimpleRegret(model=mm, sampler=sampler)
            res = acqf(X)
            self.assertEqual(res.item(), 0.0)
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
            mm._posterior._samples = mm._posterior._samples.expand(1, 2, 1)
            res = acqf(X)
            X2 = torch.zeros(
                1, 1, 1, device=self.device, dtype=dtype, requires_grad=True
            )
            with warnings.catch_warnings(record=True) as ws, settings.debug(True):
                acqf.set_X_pending(X2)
                self.assertEqual(acqf.X_pending, X2)
                self.assertEqual(
                    sum(issubclass(w.category, BotorchWarning) for w in ws), 1
                )

    def test_q_simple_regret_batch(self):
        # the event shape is `b x q x t` = 2 x 2 x 1
        for dtype in (torch.float, torch.double):
            samples = torch.zeros(2, 2, 1, device=self.device, dtype=dtype)
            samples[0, 0, 0] = 1.0
            mm = MockModel(MockPosterior(samples=samples))
            # X is a dummy and unused b/c of mocking
            X = torch.zeros(2, 2, 1, device=self.device, dtype=dtype)

            # test batch mode
            sampler = IIDNormalSampler(sample_shape=torch.Size([2]))
            acqf = qSimpleRegret(model=mm, sampler=sampler)
            res = acqf(X)
            self.assertEqual(res[0].item(), 1.0)
            self.assertEqual(res[1].item(), 0.0)

            # test batch mode
            sampler = IIDNormalSampler(sample_shape=torch.Size([2]), seed=12345)
            acqf = qSimpleRegret(model=mm, sampler=sampler)
            res = acqf(X)  # 1-dim batch
            self.assertEqual(res[0].item(), 1.0)
            self.assertEqual(res[1].item(), 0.0)
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 2, 1]))
            bs = acqf.sampler.base_samples.clone()
            acqf(X)
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))
            res = acqf(X.expand(2, -1, 1))  # 2-dim batch
            self.assertEqual(res[0].item(), 1.0)
            self.assertEqual(res[1].item(), 0.0)
            # the base samples should have the batch dim collapsed
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 2, 1]))
            bs = acqf.sampler.base_samples.clone()
            acqf(X.expand(2, -1, 1))
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))

            # test batch mode, qmc
            sampler = SobolQMCNormalSampler(sample_shape=torch.Size([2]))
            acqf = qSimpleRegret(model=mm, sampler=sampler)
            res = acqf(X)
            self.assertEqual(res[0].item(), 1.0)
            self.assertEqual(res[1].item(), 0.0)
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 2, 1]))
            bs = acqf.sampler.base_samples.clone()
            acqf(X)
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))

    # TODO: Test different objectives (incl. constraints)


class TestQUpperConfidenceBound(BotorchTestCase):
    def test_q_upper_confidence_bound(self):
        for dtype in (torch.float, torch.double):
            # the event shape is `b x q x t` = 1 x 1 x 1
            samples = torch.zeros(1, 1, 1, device=self.device, dtype=dtype)
            mm = MockModel(MockPosterior(samples=samples))
            # X is `q x d` = 1 x 1. X is a dummy and unused b/c of mocking
            X = torch.zeros(1, 1, device=self.device, dtype=dtype)

            # basic test
            sampler = IIDNormalSampler(sample_shape=torch.Size([2]))
            acqf = qUpperConfidenceBound(model=mm, beta=0.5, sampler=sampler)
            res = acqf(X)
            self.assertEqual(res.item(), 0.0)

            # basic test
            sampler = IIDNormalSampler(sample_shape=torch.Size([2]), seed=12345)
            acqf = qUpperConfidenceBound(model=mm, beta=0.5, sampler=sampler)
            res = acqf(X)
            self.assertEqual(res.item(), 0.0)
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 1, 1]))
            bs = acqf.sampler.base_samples.clone()
            res = acqf(X)
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))

            # basic test, qmc
            sampler = SobolQMCNormalSampler(sample_shape=torch.Size([2]))
            acqf = qUpperConfidenceBound(model=mm, beta=0.5, sampler=sampler)
            res = acqf(X)
            self.assertEqual(res.item(), 0.0)
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
            mm._posterior._samples = mm._posterior._samples.expand(1, 2, 1)
            res = acqf(X)
            X2 = torch.zeros(
                1, 1, 1, device=self.device, dtype=dtype, requires_grad=True
            )
            with warnings.catch_warnings(record=True) as ws, settings.debug(True):
                acqf.set_X_pending(X2)
                self.assertEqual(acqf.X_pending, X2)
                self.assertEqual(
                    sum(issubclass(w.category, BotorchWarning) for w in ws), 1
                )

    def test_q_upper_confidence_bound_batch(self):
        # TODO: T41739913 Implement tests for all MCAcquisitionFunctions
        for dtype in (torch.float, torch.double):
            samples = torch.zeros(2, 2, 1, device=self.device, dtype=dtype)
            samples[0, 0, 0] = 1.0
            mm = MockModel(MockPosterior(samples=samples))
            # X is a dummy and unused b/c of mocking
            X = torch.zeros(2, 2, 1, device=self.device, dtype=dtype)

            # test batch mode
            sampler = IIDNormalSampler(sample_shape=torch.Size([2]))
            acqf = qUpperConfidenceBound(model=mm, beta=0.5, sampler=sampler)
            res = acqf(X)
            self.assertEqual(res[0].item(), 1.0)
            self.assertEqual(res[1].item(), 0.0)

            # test batch mode
            sampler = IIDNormalSampler(sample_shape=torch.Size([2]), seed=12345)
            acqf = qUpperConfidenceBound(model=mm, beta=0.5, sampler=sampler)
            res = acqf(X)  # 1-dim batch
            self.assertEqual(res[0].item(), 1.0)
            self.assertEqual(res[1].item(), 0.0)
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 2, 1]))
            bs = acqf.sampler.base_samples.clone()
            acqf(X)
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))
            res = acqf(X.expand(2, -1, 1))  # 2-dim batch
            self.assertEqual(res[0].item(), 1.0)
            self.assertEqual(res[1].item(), 0.0)
            # the base samples should have the batch dim collapsed
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 2, 1]))
            bs = acqf.sampler.base_samples.clone()
            acqf(X.expand(2, -1, 1))
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))

            # test batch mode, qmc
            sampler = SobolQMCNormalSampler(sample_shape=torch.Size([2]))
            acqf = qUpperConfidenceBound(model=mm, beta=0.5, sampler=sampler)
            res = acqf(X)
            self.assertEqual(res[0].item(), 1.0)
            self.assertEqual(res[1].item(), 0.0)
            self.assertEqual(acqf.sampler.base_samples.shape, torch.Size([2, 1, 2, 1]))
            bs = acqf.sampler.base_samples.clone()
            acqf(X)
            self.assertTrue(torch.equal(acqf.sampler.base_samples, bs))

            # basic test for X_pending and warning
            acqf.set_X_pending()
            self.assertIsNone(acqf.X_pending)
            acqf.set_X_pending(None)
            self.assertIsNone(acqf.X_pending)
            acqf.set_X_pending(X)
            self.assertTrue(torch.equal(acqf.X_pending, X))
            mm._posterior._samples = torch.zeros(
                2, 4, 1, device=self.device, dtype=dtype
            )
            res = acqf(X)
            X2 = torch.zeros(
                1, 1, 1, device=self.device, dtype=dtype, requires_grad=True
            )
            with warnings.catch_warnings(record=True) as ws, settings.debug(True):
                acqf.set_X_pending(X2)
                self.assertEqual(acqf.X_pending, X2)
                self.assertEqual(
                    sum(issubclass(w.category, BotorchWarning) for w in ws), 1
                )

    # TODO: Test different objectives (incl. constraints)


class TestMCAcquisitionFunctionWithConstraints(BotorchTestCase):
    def test_mc_acquisition_function_with_constraints(self):
        for dtype in (torch.float, torch.double):
            with self.subTest(dtype=dtype):
                num_samples, n, q, d, m = 5, 4, 1, 3, 1
                X = torch.randn(n, q, d, device=self.device, dtype=dtype)
                samples = torch.randn(
                    num_samples, n, q, m, device=self.device, dtype=dtype
                )
                mm = MockModel(MockPosterior(samples=samples))
                nei_args = {
                    "model": mm,
                    "X_baseline": X,
                    "prune_baseline": False,
                }
                for acqf_constructor in [
                    partial(qProbabilityOfImprovement, model=mm, best_f=0.0),
                    partial(qExpectedImprovement, model=mm, best_f=0.0),
                    # cache_root=True not supported by MockModel, see test_cache_root
                    partial(qNoisyExpectedImprovement, cache_root=False, **nei_args),
                    partial(qNoisyExpectedImprovement, cache_root=True, **nei_args),
                ]:
                    acqf = acqf_constructor()
                    mm._posterior._samples = (
                        torch.cat((samples, samples), dim=-2)
                        if isinstance(acqf, qNoisyExpectedImprovement)
                        else samples
                    )
                    with self.subTest(acqf_class=type(acqf)):
                        for con in [feasible_con, infeasible_con]:
                            cacqf = acqf_constructor(constraints=[con])
                            # for NEI test
                            target = "botorch.acquisition.utils.get_infeasible_cost"
                            inf_cost = torch.tensor(3, device=self.device, dtype=dtype)
                            with mock.patch(target, return_value=inf_cost):
                                vals = cacqf(X)
                            # NOTE: this is only true for q = 1
                            expected_vals = acqf(X) * (con(samples) < 0).squeeze()
                            self.assertAllClose(vals, expected_vals)

                        with self.assertRaisesRegex(
                            ValueError,
                            "ConstrainedMCObjective as well as constraints passed",
                        ):
                            acqf_constructor(
                                constraints=[feasible_con],
                                objective=ConstrainedMCObjective(
                                    objective=IdentityMCObjective,
                                    constraints=[feasible_con],
                                ),
                            )
                # Forcing negative samples, which will throw an error with simple
                # regret because the acquisition utility is negative.
                samples = -torch.rand(n, q, m, device=self.device, dtype=dtype)
                mm = MockModel(MockPosterior(samples=samples))
                cacqf = qSimpleRegret(model=mm, constraints=[feasible_con])
                with self.assertRaisesRegex(
                    ValueError,
                    "Constraint-weighting requires unconstrained "
                    "acquisition values to be non-negative",
                ):
                    cacqf(X)

                # Test highlighting both common and different behavior of the old
                # `ConstrainedMCObjective` and new `constraints` implementation.
                # 1. Highlighting difference:
                q = 1
                samples = torch.randn(n, q, m, device=self.device, dtype=dtype)
                mm = MockModel(MockPosterior(samples=samples))
                constrained_objective = ConstrainedMCObjective(
                    objective=IdentityMCObjective(),
                    constraints=[infeasible_con],
                    infeasible_cost=0.0,
                )
                # The old `ConstrainedMCObjective`-based implementation does not scale
                # the best_f value by the feasibility indicator, while the new
                # `constraints`-based implementation does. Therefore, the old version
                # yields an acquisition value of 1, even though the constraint is not
                # satisfied.
                best_f = -1.0
                old_acqf = qExpectedImprovement(
                    model=mm, best_f=best_f, objective=constrained_objective
                )
                new_acqf = qExpectedImprovement(
                    model=mm, best_f=best_f, constraints=[infeasible_con]
                )
                old_val = old_acqf(X)
                self.assertAllClose(old_val, torch.ones_like(old_val))
                new_val = new_acqf(X)
                self.assertAllClose(new_val, torch.zeros_like(new_val))

                # 2. Highlighting commonality:
                # When best_f = 0 and infeasible_cost = 0, both implementations yield
                # the same results.
                constrained_objective = ConstrainedMCObjective(
                    objective=IdentityMCObjective(),
                    constraints=[feasible_con],
                    infeasible_cost=0.0,
                )
                best_f = 0.0
                old_acqf = qExpectedImprovement(
                    model=mm, best_f=best_f, objective=constrained_objective
                )
                new_acqf = qExpectedImprovement(
                    model=mm, best_f=best_f, constraints=[feasible_con]
                )
                old_val = old_acqf(X)
                new_val = new_acqf(X)
                self.assertAllClose(new_val, old_val)
