#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from itertools import product

import torch
from botorch.acquisition.analytic import ExpectedImprovement
from botorch.acquisition.monte_carlo import qExpectedImprovement
from botorch.acquisition.prior_guided import PriorGuidedAcquisitionFunction
from botorch.models import SingleTaskGP
from botorch.utils.testing import BotorchTestCase
from botorch.utils.transforms import match_batch_shape
from torch.nn import Module


class DummyPrior(Module):
    def forward(self, X):
        p = torch.distributions.Normal(0, 1)
        # sum over d dimensions
        return p.log_prob(X).sum(dim=-1).exp()


def get_val_prob(test_X, test_X_exp, af, prior):
    with torch.no_grad():
        val = af(test_X)
        prob = prior(test_X_exp)

    return val, prob


def get_weighted_val(ei_val, prob, exponent, use_log):
    if use_log:
        return prob * exponent + ei_val
    return prob.pow(exponent) * ei_val


class TestPriorGuidedAcquisitionFunction(BotorchTestCase):
    def setUp(self):
        super().setUp()
        self.prior = DummyPrior()
        self.train_X = torch.rand(5, 3, dtype=torch.double, device=self.device)
        self.train_Y = self.train_X.norm(dim=-1, keepdim=True)

    def test_prior_guided_analytic_acquisition_function(self):
        for dtype in (torch.float, torch.double):
            model = SingleTaskGP(
                self.train_X.to(dtype=dtype), self.train_Y.to(dtype=dtype)
            )
            ei = ExpectedImprovement(model, best_f=0.0)
            for batch_shape, use_log, exponent in product(
                ([], [2]),
                (False, True),
                (1.0, 2.0),
            ):
                af = PriorGuidedAcquisitionFunction(
                    acq_function=ei,
                    prior_module=self.prior,
                    log=use_log,
                    prior_exponent=exponent,
                )
                test_X = torch.rand(*batch_shape, 1, 3, dtype=dtype, device=self.device)
                test_X_exp = test_X.unsqueeze(0) if batch_shape == [] else test_X
                with torch.no_grad():
                    ei_val = ei(test_X_exp).unsqueeze(-1)
                val, prob = get_val_prob(test_X, test_X_exp, af, self.prior)
                weighted_val = get_weighted_val(ei_val, prob, exponent, use_log)
                expected_val = weighted_val.squeeze(-1)
                self.assertTrue(torch.allclose(val, expected_val))
                # test that q>1 and a non SampleReducing AF raises an exception
                msg = (
                    "q-batches with q>1 are only supported using "
                    "SampleReducingMCAcquisitionFunction."
                )
                test_X = torch.rand(2, 3, dtype=dtype, device=self.device)
                with self.assertRaisesRegex(NotImplementedError, msg):
                    af(test_X)

    def test_prior_guided_mc_acquisition_function(self):
        for dtype in (torch.float, torch.double):
            model = SingleTaskGP(
                self.train_X.to(dtype=dtype), self.train_Y.to(dtype=dtype)
            )
            ei = qExpectedImprovement(model, best_f=0.0)
            for batch_shape, q, use_log, exponent in product(
                ([], [2]),
                (1, 2),
                (False, True),
                (1.0, 2.0),
            ):
                af = PriorGuidedAcquisitionFunction(
                    acq_function=ei,
                    prior_module=self.prior,
                    log=use_log,
                    prior_exponent=exponent,
                )
                test_X = torch.rand(*batch_shape, q, 3, dtype=dtype, device=self.device)
                test_X_exp = test_X.unsqueeze(0) if batch_shape == [] else test_X
                val, prob = get_val_prob(test_X, test_X_exp, af, self.prior)
                ei_val = ei._non_reduced_forward(test_X_exp)
                weighted_val = get_weighted_val(ei_val, prob, exponent, use_log)
                expected_val = ei._sample_reduction(ei._q_reduction(weighted_val))
                self.assertTrue(torch.allclose(val, expected_val))
                # test set_X_pending
                X_pending = torch.rand(2, 3, dtype=dtype, device=self.device)
                af.X_pending = X_pending
                self.assertTrue(torch.equal(X_pending, af.X_pending))
                # unsqueeze batch dim
                test_X_exp_with_pending = torch.cat(
                    [test_X_exp, match_batch_shape(X_pending, test_X_exp)], dim=-2
                )
                with torch.no_grad():
                    val = af(test_X)
                    prob = self.prior(test_X_exp_with_pending)
                    ei_val = ei._non_reduced_forward(test_X_exp_with_pending)
                if use_log:
                    weighted_val = prob * exponent + ei_val
                else:
                    weighted_val = prob.pow(exponent) * ei_val
                expected_val = ei._sample_reduction(ei._q_reduction(weighted_val))

                self.assertTrue(torch.equal(val, expected_val))
