#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

import math

from typing import Any, Callable, List, Type
from unittest import mock

import torch
from botorch.acquisition.acquisition import AcquisitionFunction
from botorch.acquisition.analytic import (
    ExpectedImprovement,
    LogExpectedImprovement,
    LogNoisyExpectedImprovement,
    LogProbabilityOfImprovement,
    NoisyExpectedImprovement,
    PosteriorMean,
    ProbabilityOfImprovement,
    UpperConfidenceBound,
)
from botorch.acquisition.fixed_feature import FixedFeatureAcquisitionFunction
from botorch.acquisition.input_constructors import (
    _field_is_shared,
    acqf_input_constructor,
    construct_inputs_mf_base,
    get_acqf_input_constructor,
    get_best_f_analytic,
    get_best_f_mc,
)
from botorch.acquisition.joint_entropy_search import qJointEntropySearch
from botorch.acquisition.knowledge_gradient import (
    qKnowledgeGradient,
    qMultiFidelityKnowledgeGradient,
)
from botorch.acquisition.logei import (
    qLogExpectedImprovement,
    qLogNoisyExpectedImprovement,
    TAU_MAX,
    TAU_RELU,
)
from botorch.acquisition.max_value_entropy_search import (
    qMaxValueEntropy,
    qMultiFidelityMaxValueEntropy,
)
from botorch.acquisition.monte_carlo import (
    qExpectedImprovement,
    qNoisyExpectedImprovement,
    qProbabilityOfImprovement,
    qSimpleRegret,
    qUpperConfidenceBound,
)
from botorch.acquisition.multi_objective import (
    ExpectedHypervolumeImprovement,
    qExpectedHypervolumeImprovement,
    qNoisyExpectedHypervolumeImprovement,
)
from botorch.acquisition.multi_objective.multi_output_risk_measures import (
    MultiOutputExpectation,
)
from botorch.acquisition.multi_objective.objective import (
    IdentityAnalyticMultiOutputObjective,
    IdentityMCMultiOutputObjective,
    WeightedMCMultiOutputObjective,
)
from botorch.acquisition.multi_objective.utils import get_default_partitioning_alpha
from botorch.acquisition.objective import (
    LinearMCObjective,
    ScalarizedPosteriorTransform,
)
from botorch.acquisition.preference import AnalyticExpectedUtilityOfBestOption
from botorch.acquisition.utils import (
    expand_trace_observations,
    project_to_target_fidelity,
)
from botorch.exceptions.errors import UnsupportedError
from botorch.models import FixedNoiseGP, MultiTaskGP, SingleTaskGP
from botorch.models.deterministic import FixedSingleSampleModel
from botorch.models.model_list_gp_regression import ModelListGP
from botorch.sampling.normal import IIDNormalSampler, SobolQMCNormalSampler
from botorch.utils.constraints import get_outcome_constraint_transforms
from botorch.utils.datasets import SupervisedDataset
from botorch.utils.multi_objective.box_decompositions.non_dominated import (
    FastNondominatedPartitioning,
    NondominatedPartitioning,
)
from botorch.utils.testing import BotorchTestCase, MockModel, MockPosterior


class DummyAcquisitionFunction(AcquisitionFunction):
    ...


class InputConstructorBaseTestCase:
    def setUp(self) -> None:
        super().setUp()
        self.mock_model = MockModel(
            posterior=MockPosterior(mean=None, variance=None, base_shape=(1,))
        )

        X1 = torch.rand(3, 2)
        X2 = torch.rand(3, 2)
        Y1 = torch.rand(3, 1)
        Y2 = torch.rand(3, 1)

        self.blockX_blockY = SupervisedDataset.dict_from_iter(X1, Y1)
        self.blockX_multiY = SupervisedDataset.dict_from_iter(X1, (Y1, Y2))
        self.multiX_multiY = SupervisedDataset.dict_from_iter((X1, X2), (Y1, Y2))
        self.bounds = 2 * [(0.0, 1.0)]


class TestInputConstructorUtils(InputConstructorBaseTestCase, BotorchTestCase):
    def test_field_is_shared(self) -> None:
        self.assertTrue(_field_is_shared(self.blockX_multiY, "X"))
        self.assertFalse(_field_is_shared(self.blockX_multiY, "Y"))
        with self.assertRaisesRegex(AttributeError, "has no field"):
            self.assertFalse(_field_is_shared(self.blockX_multiY, "foo"))

    def test_get_best_f_analytic(self) -> None:
        with self.assertRaisesRegex(
            NotImplementedError, "Currently only block designs are supported."
        ):
            get_best_f_analytic(training_data=self.multiX_multiY)

        best_f = get_best_f_analytic(training_data=self.blockX_blockY)
        self.assertEqual(best_f, get_best_f_analytic(self.blockX_blockY[0]))

        best_f_expected = self.blockX_blockY[0].Y().squeeze().max()
        self.assertEqual(best_f, best_f_expected)
        with self.assertRaisesRegex(
            NotImplementedError,
            "Analytic acquisition functions currently only work with "
            "multi-output models if provided with a",
        ):
            get_best_f_analytic(training_data=self.blockX_multiY)
        weights = torch.rand(2)

        post_tf = ScalarizedPosteriorTransform(weights=weights)
        best_f_tf = get_best_f_analytic(
            training_data=self.blockX_multiY, posterior_transform=post_tf
        )

        multi_Y = torch.cat([d.Y() for d in self.blockX_multiY.values()], dim=-1)
        best_f_expected = post_tf.evaluate(multi_Y).max()
        self.assertEqual(best_f_tf, best_f_expected)

    def test_get_best_f_mc(self) -> None:
        with self.assertRaisesRegex(
            NotImplementedError, "Currently only block designs are supported."
        ):
            get_best_f_mc(training_data=self.multiX_multiY)

        best_f = get_best_f_mc(training_data=self.blockX_blockY)
        self.assertEqual(best_f, get_best_f_mc(self.blockX_blockY[0]))

        best_f_expected = self.blockX_blockY[0].Y().max(dim=0).values
        self.assertAllClose(best_f, best_f_expected)
        with self.assertRaisesRegex(UnsupportedError, "require an objective"):
            get_best_f_mc(training_data=self.blockX_multiY)
        obj = LinearMCObjective(weights=torch.rand(2))
        best_f = get_best_f_mc(training_data=self.blockX_multiY, objective=obj)

        multi_Y = torch.cat([d.Y() for d in self.blockX_multiY.values()], dim=-1)
        best_f_expected = (multi_Y @ obj.weights).amax(dim=-1, keepdim=True)
        self.assertAllClose(best_f, best_f_expected)
        post_tf = ScalarizedPosteriorTransform(weights=torch.ones(2))
        best_f = get_best_f_mc(
            training_data=self.blockX_multiY, posterior_transform=post_tf
        )
        best_f_expected = (multi_Y.sum(dim=-1)).amax(dim=-1, keepdim=True)
        self.assertAllClose(best_f, best_f_expected)

    @mock.patch("botorch.acquisition.input_constructors.optimize_acqf")
    def test_optimize_objective(self, mock_optimize_acqf):
        from botorch.acquisition.input_constructors import optimize_objective

        mock_model = self.mock_model
        bounds = torch.rand(2, len(self.bounds))

        A = torch.rand(1, bounds.shape[-1])
        b = torch.zeros([1, 1])
        idx = A[0].nonzero(as_tuple=False).squeeze()
        inequality_constraints = ((idx, -A[0, idx], -b[0, 0]),)

        with self.subTest("scalarObjective_linearConstraints"):
            post_tf = ScalarizedPosteriorTransform(weights=torch.rand(bounds.shape[-1]))
            _ = optimize_objective(
                model=mock_model,
                bounds=bounds,
                q=1,
                posterior_transform=post_tf,
                linear_constraints=(A, b),
                fixed_features=None,
            )

            kwargs = mock_optimize_acqf.call_args[1]
            self.assertIsInstance(kwargs["acq_function"], PosteriorMean)
            self.assertTrue(torch.equal(kwargs["bounds"], bounds))
            self.assertEqual(len(kwargs["inequality_constraints"]), 1)
            for a, b in zip(
                kwargs["inequality_constraints"][0], inequality_constraints[0]
            ):
                self.assertTrue(torch.equal(a, b))

        with self.subTest("mcObjective_fixedFeatures"):
            _ = optimize_objective(
                model=mock_model,
                bounds=bounds,
                q=1,
                objective=LinearMCObjective(weights=torch.rand(bounds.shape[-1])),
                fixed_features={0: 0.5},
            )

            kwargs = mock_optimize_acqf.call_args[1]
            self.assertIsInstance(
                kwargs["acq_function"], FixedFeatureAcquisitionFunction
            )
            self.assertIsInstance(kwargs["acq_function"].acq_func, qSimpleRegret)
            self.assertTrue(torch.equal(kwargs["bounds"], bounds[:, 1:]))

    def test__allow_only_specific_variable_kwargs__raises(self) -> None:
        input_constructor = get_acqf_input_constructor(ExpectedImprovement)
        with self.assertRaisesRegex(
            TypeError,
            "Unexpected keyword argument `hat` when constructing input arguments",
        ):
            input_constructor(
                model=self.mock_model, training_data=self.blockX_blockY, hat="car"
            )


class TestAnalyticAcquisitionFunctionInputConstructors(
    InputConstructorBaseTestCase, BotorchTestCase
):
    def test_acqf_input_constructor(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "not registered"):
            get_acqf_input_constructor(DummyAcquisitionFunction)

        with self.assertRaisesRegex(ValueError, "duplicate"):
            acqf_input_constructor(ExpectedImprovement)(lambda x: x)

    def test_construct_inputs_posterior_mean(self) -> None:
        c = get_acqf_input_constructor(PosteriorMean)
        mock_model = self.mock_model
        kwargs = c(model=mock_model, training_data=self.blockX_blockY)
        self.assertIs(kwargs["model"], mock_model)
        self.assertIsNone(kwargs["posterior_transform"])
        # test instantiation
        acqf = PosteriorMean(**kwargs)
        self.assertIs(acqf.model, mock_model)

        post_tf = ScalarizedPosteriorTransform(weights=torch.rand(1))
        kwargs = c(
            model=mock_model,
            training_data=self.blockX_blockY,
            posterior_transform=post_tf,
        )
        self.assertIs(kwargs["model"], mock_model)
        self.assertIs(kwargs["posterior_transform"], post_tf)
        # test instantiation
        acqf = PosteriorMean(**kwargs)
        self.assertIs(acqf.model, mock_model)

    def test_construct_inputs_best_f(self) -> None:
        for acqf_cls in [
            ExpectedImprovement,
            LogExpectedImprovement,
            ProbabilityOfImprovement,
            LogProbabilityOfImprovement,
        ]:
            with self.subTest(acqf_cls=acqf_cls):
                c = get_acqf_input_constructor(acqf_cls)
                mock_model = self.mock_model
                kwargs = c(
                    model=mock_model, training_data=self.blockX_blockY, maximize=False
                )
                best_f_expected = self.blockX_blockY[0].Y().squeeze().max()
                self.assertIs(kwargs["model"], mock_model)
                self.assertIsNone(kwargs["posterior_transform"])
                self.assertEqual(kwargs["best_f"], best_f_expected)
                self.assertFalse(kwargs["maximize"])
                acqf = acqf_cls(**kwargs)
                self.assertIs(acqf.model, mock_model)

                kwargs = c(
                    model=mock_model, training_data=self.blockX_blockY, best_f=0.1
                )
                self.assertIs(kwargs["model"], mock_model)
                self.assertIsNone(kwargs["posterior_transform"])
                self.assertEqual(kwargs["best_f"], 0.1)
                self.assertTrue(kwargs["maximize"])
                acqf = acqf_cls(**kwargs)
                self.assertIs(acqf.model, mock_model)

    def test_construct_inputs_ucb(self) -> None:
        c = get_acqf_input_constructor(UpperConfidenceBound)
        mock_model = self.mock_model
        kwargs = c(model=mock_model, training_data=self.blockX_blockY)
        self.assertIs(kwargs["model"], mock_model)
        self.assertIsNone(kwargs["posterior_transform"])
        self.assertEqual(kwargs["beta"], 0.2)
        self.assertTrue(kwargs["maximize"])
        acqf = UpperConfidenceBound(**kwargs)
        self.assertIs(mock_model, acqf.model)

        kwargs = c(
            model=mock_model, training_data=self.blockX_blockY, beta=0.1, maximize=False
        )
        self.assertIs(kwargs["model"], mock_model)
        self.assertIsNone(kwargs["posterior_transform"])
        self.assertEqual(kwargs["beta"], 0.1)
        self.assertFalse(kwargs["maximize"])
        acqf = UpperConfidenceBound(**kwargs)
        self.assertIs(mock_model, acqf.model)

    def test_construct_inputs_noisy_ei(self) -> None:
        for acqf_cls in [NoisyExpectedImprovement, LogNoisyExpectedImprovement]:
            with self.subTest(acqf_cls=acqf_cls):
                c = get_acqf_input_constructor(acqf_cls)
                mock_model = FixedNoiseGP(
                    train_X=torch.rand((2, 2)),
                    train_Y=torch.rand((2, 1)),
                    train_Yvar=torch.rand((2, 1)),
                )
                kwargs = c(model=mock_model, training_data=self.blockX_blockY)
                self.assertEqual(kwargs["model"], mock_model)
                self.assertTrue(
                    torch.equal(kwargs["X_observed"], self.blockX_blockY[0].X())
                )
                self.assertEqual(kwargs["num_fantasies"], 20)
                self.assertTrue(kwargs["maximize"])
                acqf = acqf_cls(**kwargs)
                self.assertTrue(acqf.maximize)

                kwargs = c(
                    model=mock_model,
                    training_data=self.blockX_blockY,
                    num_fantasies=10,
                    maximize=False,
                )
                self.assertEqual(kwargs["model"], mock_model)
                self.assertTrue(
                    torch.equal(kwargs["X_observed"], self.blockX_blockY[0].X())
                )
                self.assertEqual(kwargs["num_fantasies"], 10)
                self.assertFalse(kwargs["maximize"])
                acqf = acqf_cls(**kwargs)
                self.assertFalse(acqf.maximize)

                with self.assertRaisesRegex(ValueError, "Field `X` must be shared"):
                    c(model=mock_model, training_data=self.multiX_multiY)

    def test_construct_inputs_constrained_analytic_eubo(self) -> None:
        # create dummy modellist gp
        n = 10
        X = torch.linspace(0, 0.95, n).unsqueeze(dim=-1)
        Y1, Y2 = torch.sin(X * (2 * math.pi)), torch.cos(X * (2 * math.pi))
        # 3 tasks
        train_X = torch.cat(
            [torch.nn.functional.pad(X, (1, 0), value=i) for i in range(3)]
        )
        train_Y = torch.cat([Y1, Y2])  # train_Y is a 1d tensor with shape (2n,)
        # model list of 2, so model.num_outputs is 4
        model = ModelListGP(
            *[MultiTaskGP(train_X, train_Y, task_feature=0) for i in range(2)]
        )
        self.assertEqual(model.num_outputs, 6)

        c = get_acqf_input_constructor(AnalyticExpectedUtilityOfBestOption)
        mock_pref_model = self.mock_model
        # assume we only have a preference model with 2 outcomes
        mock_pref_model.dim = 2
        mock_pref_model.datapoints = torch.tensor([])

        # test basic construction
        kwargs = c(model=model, pref_model=mock_pref_model)
        self.assertIsInstance(kwargs["outcome_model"], FixedSingleSampleModel)
        self.assertIs(kwargs["pref_model"], mock_pref_model)
        self.assertIsNone(kwargs["previous_winner"])
        # test instantiation
        AnalyticExpectedUtilityOfBestOption(**kwargs)

        # test previous_winner
        previous_winner = torch.randn(mock_pref_model.dim)
        kwargs = c(
            model=model,
            pref_model=mock_pref_model,
            previous_winner=previous_winner,
        )
        self.assertTrue(torch.equal(kwargs["previous_winner"], previous_winner))
        # test instantiation
        AnalyticExpectedUtilityOfBestOption(**kwargs)

        # test sample_multiplier
        torch.manual_seed(123)
        kwargs = c(
            model=model,
            pref_model=mock_pref_model,
            sample_multiplier=1e6,
        )
        # w by default is drawn from std normal and very unlikely to be > 10.0
        self.assertTrue((kwargs["outcome_model"].w.abs() > 10.0).all())
        # Check w has the right dimension that agrees with the preference model
        self.assertEqual(kwargs["outcome_model"].w.shape[-1], mock_pref_model.dim)


class TestMCAcquisitionFunctionInputConstructors(
    InputConstructorBaseTestCase, BotorchTestCase
):
    def test_construct_inputs_mc_base(self) -> None:
        c = get_acqf_input_constructor(qSimpleRegret)
        mock_model = self.mock_model
        kwargs = c(model=mock_model, training_data=self.blockX_blockY)
        self.assertIs(kwargs["model"], mock_model)
        self.assertIsNone(kwargs["objective"])
        self.assertIsNone(kwargs["X_pending"])
        self.assertIsNone(kwargs["sampler"])
        acqf = qSimpleRegret(**kwargs)
        self.assertIs(acqf.model, mock_model)

        X_pending = torch.rand(2, 2)
        objective = LinearMCObjective(torch.rand(2))
        kwargs = c(
            model=mock_model,
            training_data=self.blockX_blockY,
            objective=objective,
            X_pending=X_pending,
        )
        self.assertIs(kwargs["model"], mock_model)
        self.assertTrue(torch.equal(kwargs["objective"].weights, objective.weights))
        self.assertTrue(torch.equal(kwargs["X_pending"], X_pending))
        self.assertIsNone(kwargs["sampler"])
        acqf = qSimpleRegret(**kwargs)
        self.assertIs(acqf.model, mock_model)
        # TODO: Test passing through of sampler

    def test_construct_inputs_qEI(self) -> None:
        c = get_acqf_input_constructor(qExpectedImprovement)
        mock_model = self.mock_model
        kwargs = c(model=mock_model, training_data=self.blockX_blockY)
        self.assertIs(kwargs["model"], mock_model)
        self.assertIsNone(kwargs["objective"])
        self.assertIsNone(kwargs["X_pending"])
        self.assertIsNone(kwargs["sampler"])
        self.assertIsNone(kwargs["constraints"])
        self.assertIsInstance(kwargs["eta"], float)
        self.assertLess(kwargs["eta"], 1)
        acqf = qExpectedImprovement(**kwargs)
        self.assertIs(acqf.model, mock_model)

        X_pending = torch.rand(2, 2)
        objective = LinearMCObjective(torch.rand(2))
        kwargs = c(
            model=mock_model,
            training_data=self.blockX_multiY,
            objective=objective,
            X_pending=X_pending,
        )
        self.assertIs(kwargs["model"], mock_model)
        self.assertTrue(torch.equal(kwargs["objective"].weights, objective.weights))
        self.assertTrue(torch.equal(kwargs["X_pending"], X_pending))
        self.assertIsNone(kwargs["sampler"])
        self.assertIsInstance(kwargs["eta"], float)
        self.assertLess(kwargs["eta"], 1)
        acqf = qExpectedImprovement(**kwargs)
        self.assertIs(acqf.model, mock_model)

        multi_Y = torch.cat([d.Y() for d in self.blockX_multiY.values()], dim=-1)
        best_f_expected = objective(multi_Y).max()
        self.assertEqual(kwargs["best_f"], best_f_expected)
        # Check explicitly specifying `best_f`.
        best_f_expected = best_f_expected - 1  # Random value.
        kwargs = c(
            model=mock_model,
            training_data=self.blockX_multiY,
            objective=objective,
            X_pending=X_pending,
            best_f=best_f_expected,
        )
        self.assertEqual(kwargs["best_f"], best_f_expected)
        acqf = qExpectedImprovement(**kwargs)
        self.assertIs(acqf.model, mock_model)
        self.assertEqual(acqf.best_f, best_f_expected)

        # test passing constraints
        outcome_constraints = (torch.tensor([[0.0, 1.0]]), torch.tensor([[0.5]]))
        constraints = get_outcome_constraint_transforms(
            outcome_constraints=outcome_constraints
        )
        kwargs = c(
            model=mock_model,
            training_data=self.blockX_multiY,
            objective=objective,
            X_pending=X_pending,
            best_f=best_f_expected,
            constraints=constraints,
        )
        self.assertIs(kwargs["constraints"], constraints)
        acqf = qExpectedImprovement(**kwargs)
        self.assertEqual(acqf.best_f, best_f_expected)

        # testing qLogEI input constructor
        log_constructor = get_acqf_input_constructor(qLogExpectedImprovement)
        log_kwargs = log_constructor(
            model=mock_model,
            training_data=self.blockX_blockY,
            objective=objective,
            X_pending=X_pending,
            best_f=best_f_expected,
            constraints=constraints,
        )
        # includes strict superset of kwargs tested above
        self.assertLessEqual(kwargs.items(), log_kwargs.items())
        self.assertIn("fat", log_kwargs)
        self.assertIn("tau_max", log_kwargs)
        self.assertEqual(log_kwargs["tau_max"], TAU_MAX)
        self.assertIn("tau_relu", log_kwargs)
        self.assertEqual(log_kwargs["tau_relu"], TAU_RELU)
        self.assertIs(log_kwargs["constraints"], constraints)
        acqf = qLogExpectedImprovement(**log_kwargs)
        self.assertIs(acqf.model, mock_model)
        self.assertIs(acqf.objective, objective)

    def test_construct_inputs_qNEI(self) -> None:
        c = get_acqf_input_constructor(qNoisyExpectedImprovement)
        mock_model = SingleTaskGP(
            train_X=torch.rand((2, 2)), train_Y=torch.rand((2, 1))
        )
        kwargs = c(model=mock_model, training_data=self.blockX_blockY)
        self.assertIs(kwargs["model"], mock_model)
        self.assertIsNone(kwargs["objective"])
        self.assertIsNone(kwargs["X_pending"])
        self.assertIsNone(kwargs["sampler"])
        self.assertTrue(kwargs["prune_baseline"])
        self.assertTrue(torch.equal(kwargs["X_baseline"], self.blockX_blockY[0].X()))
        self.assertIsNone(kwargs["constraints"])
        self.assertIsInstance(kwargs["eta"], float)
        self.assertLess(kwargs["eta"], 1)
        acqf = qNoisyExpectedImprovement(**kwargs)
        self.assertIs(acqf.model, mock_model)

        with self.assertRaisesRegex(ValueError, "Field `X` must be shared"):
            c(model=mock_model, training_data=self.multiX_multiY)

        X_baseline = torch.rand(2, 2)
        outcome_constraints = (torch.tensor([[0.0, 1.0]]), torch.tensor([[0.5]]))
        constraints = get_outcome_constraint_transforms(
            outcome_constraints=outcome_constraints
        )
        kwargs = c(
            model=mock_model,
            training_data=self.blockX_blockY,
            X_baseline=X_baseline,
            prune_baseline=False,
            constraints=constraints,
        )
        self.assertEqual(kwargs["model"], mock_model)
        self.assertIsNone(kwargs["objective"])
        self.assertIsNone(kwargs["X_pending"])
        self.assertIsNone(kwargs["sampler"])
        self.assertFalse(kwargs["prune_baseline"])
        self.assertTrue(torch.equal(kwargs["X_baseline"], X_baseline))
        self.assertIsInstance(kwargs["eta"], float)
        self.assertLess(kwargs["eta"], 1)
        self.assertIs(kwargs["constraints"], constraints)
        acqf = qNoisyExpectedImprovement(**kwargs)
        self.assertIs(acqf.model, mock_model)

        # testing qLogNEI input constructor
        log_constructor = get_acqf_input_constructor(qLogNoisyExpectedImprovement)

        log_kwargs = log_constructor(
            model=mock_model,
            training_data=self.blockX_blockY,
            X_baseline=X_baseline,
            prune_baseline=False,
            constraints=constraints,
        )
        # includes strict superset of kwargs tested above
        self.assertLessEqual(kwargs.items(), log_kwargs.items())
        self.assertIn("fat", log_kwargs)
        self.assertIn("tau_max", log_kwargs)
        self.assertEqual(log_kwargs["tau_max"], TAU_MAX)
        self.assertIn("tau_relu", log_kwargs)
        self.assertEqual(log_kwargs["tau_relu"], TAU_RELU)
        self.assertIs(log_kwargs["constraints"], constraints)
        acqf = qLogNoisyExpectedImprovement(**log_kwargs)
        self.assertIs(acqf.model, mock_model)

    def test_construct_inputs_qPI(self) -> None:
        c = get_acqf_input_constructor(qProbabilityOfImprovement)
        mock_model = self.mock_model
        kwargs = c(model=mock_model, training_data=self.blockX_blockY)
        self.assertEqual(kwargs["model"], mock_model)
        self.assertIsNone(kwargs["objective"])
        self.assertIsNone(kwargs["X_pending"])
        self.assertIsNone(kwargs["sampler"])
        self.assertEqual(kwargs["tau"], 1e-3)
        self.assertIsNone(kwargs["constraints"])
        self.assertIsInstance(kwargs["eta"], float)
        self.assertLess(kwargs["eta"], 1)
        acqf = qProbabilityOfImprovement(**kwargs)
        self.assertIs(acqf.model, mock_model)

        X_pending = torch.rand(2, 2)
        objective = LinearMCObjective(torch.rand(2))
        kwargs = c(
            model=mock_model,
            training_data=self.blockX_multiY,
            objective=objective,
            X_pending=X_pending,
            tau=1e-2,
        )
        self.assertEqual(kwargs["model"], mock_model)
        self.assertTrue(torch.equal(kwargs["objective"].weights, objective.weights))
        self.assertTrue(torch.equal(kwargs["X_pending"], X_pending))
        self.assertIsNone(kwargs["sampler"])
        self.assertEqual(kwargs["tau"], 1e-2)
        self.assertIsInstance(kwargs["eta"], float)
        self.assertLess(kwargs["eta"], 1)
        multi_Y = torch.cat([d.Y() for d in self.blockX_multiY.values()], dim=-1)
        best_f_expected = objective(multi_Y).max()
        self.assertEqual(kwargs["best_f"], best_f_expected)
        acqf = qProbabilityOfImprovement(**kwargs)
        self.assertIs(acqf.model, mock_model)
        self.assertIs(acqf.objective, objective)

        # Check explicitly specifying `best_f`.
        best_f_expected = best_f_expected - 1  # Random value.
        outcome_constraints = (torch.tensor([[0.0, 1.0]]), torch.tensor([[0.5]]))
        constraints = get_outcome_constraint_transforms(
            outcome_constraints=outcome_constraints
        )
        kwargs = c(
            model=mock_model,
            training_data=self.blockX_multiY,
            objective=objective,
            X_pending=X_pending,
            tau=1e-2,
            best_f=best_f_expected,
            constraints=constraints,
        )
        self.assertEqual(kwargs["best_f"], best_f_expected)
        self.assertIs(kwargs["constraints"], constraints)
        acqf = qProbabilityOfImprovement(**kwargs)
        self.assertIs(acqf.model, mock_model)
        self.assertIs(acqf.objective, objective)

    def test_construct_inputs_qUCB(self) -> None:
        c = get_acqf_input_constructor(qUpperConfidenceBound)
        mock_model = self.mock_model
        kwargs = c(model=mock_model, training_data=self.blockX_blockY)
        self.assertEqual(kwargs["model"], mock_model)
        self.assertIsNone(kwargs["objective"])
        self.assertIsNone(kwargs["X_pending"])
        self.assertIsNone(kwargs["sampler"])
        self.assertEqual(kwargs["beta"], 0.2)
        acqf = qUpperConfidenceBound(**kwargs)
        self.assertIs(acqf.model, mock_model)

        X_pending = torch.rand(2, 2)
        objective = LinearMCObjective(torch.rand(2))
        kwargs = c(
            model=mock_model,
            training_data=self.blockX_blockY,
            objective=objective,
            X_pending=X_pending,
            beta=0.1,
        )
        self.assertEqual(kwargs["model"], mock_model)
        self.assertTrue(torch.equal(kwargs["objective"].weights, objective.weights))
        self.assertTrue(torch.equal(kwargs["X_pending"], X_pending))
        self.assertIsNone(kwargs["sampler"])
        self.assertEqual(kwargs["beta"], 0.1)
        acqf = qUpperConfidenceBound(**kwargs)
        self.assertIs(acqf.model, mock_model)


class TestMultiObjectiveAcquisitionFunctionInputConstructors(
    InputConstructorBaseTestCase, BotorchTestCase
):
    def test_construct_inputs_EHVI(self) -> None:
        c = get_acqf_input_constructor(ExpectedHypervolumeImprovement)
        mock_model = mock.Mock()
        objective_thresholds = torch.rand(6)

        # test error on non-block designs
        with self.assertRaisesRegex(ValueError, "Field `X` must be shared"):
            c(
                model=mock_model,
                training_data=self.multiX_multiY,
                objective_thresholds=objective_thresholds,
            )

        # test error on unsupported outcome constraints
        with self.assertRaises(NotImplementedError):
            c(
                model=mock_model,
                training_data=self.blockX_blockY,
                objective_thresholds=objective_thresholds,
                constraints=mock.Mock(),
            )

        # test with Y_pmean supplied explicitly
        Y_pmean = torch.rand(3, 6)
        kwargs = c(
            model=mock_model,
            training_data=self.blockX_blockY,
            objective_thresholds=objective_thresholds,
            Y_pmean=Y_pmean,
        )
        self.assertEqual(kwargs["model"], mock_model)
        self.assertIsInstance(kwargs["objective"], IdentityAnalyticMultiOutputObjective)
        self.assertTrue(torch.equal(kwargs["ref_point"], objective_thresholds))
        partitioning = kwargs["partitioning"]
        alpha_expected = get_default_partitioning_alpha(6)
        self.assertIsInstance(partitioning, NondominatedPartitioning)
        self.assertEqual(partitioning.alpha, alpha_expected)
        self.assertTrue(torch.equal(partitioning._neg_ref_point, -objective_thresholds))

        Y_pmean = torch.rand(3, 2)
        objective_thresholds = torch.rand(2)
        kwargs = c(
            model=mock_model,
            training_data=self.blockX_blockY,
            objective_thresholds=objective_thresholds,
            Y_pmean=Y_pmean,
        )
        partitioning = kwargs["partitioning"]
        self.assertIsInstance(partitioning, FastNondominatedPartitioning)
        self.assertTrue(torch.equal(partitioning.ref_point, objective_thresholds))

        # test with custom objective
        weights = torch.rand(2)
        obj = WeightedMCMultiOutputObjective(weights=weights)
        kwargs = c(
            model=mock_model,
            training_data=self.blockX_blockY,
            objective_thresholds=objective_thresholds,
            objective=obj,
            Y_pmean=Y_pmean,
            alpha=0.05,
        )
        self.assertEqual(kwargs["model"], mock_model)
        self.assertIsInstance(kwargs["objective"], WeightedMCMultiOutputObjective)
        ref_point_expected = objective_thresholds * weights
        self.assertTrue(torch.equal(kwargs["ref_point"], ref_point_expected))
        partitioning = kwargs["partitioning"]
        self.assertIsInstance(partitioning, NondominatedPartitioning)
        self.assertEqual(partitioning.alpha, 0.05)
        self.assertTrue(torch.equal(partitioning._neg_ref_point, -ref_point_expected))

        # Test without providing Y_pmean (computed from model)
        mean = torch.rand(1, 2)
        variance = torch.ones(1, 1)
        mm = MockModel(MockPosterior(mean=mean, variance=variance))
        kwargs = c(
            model=mm,
            training_data=self.blockX_blockY,
            objective_thresholds=objective_thresholds,
        )
        self.assertIsInstance(kwargs["objective"], IdentityAnalyticMultiOutputObjective)
        self.assertTrue(torch.equal(kwargs["ref_point"], objective_thresholds))
        partitioning = kwargs["partitioning"]
        self.assertIsInstance(partitioning, FastNondominatedPartitioning)
        self.assertTrue(torch.equal(partitioning.ref_point, objective_thresholds))
        self.assertTrue(torch.equal(partitioning._neg_Y, -mean))

        # Test with risk measures.
        for use_preprocessing in (True, False):
            obj = MultiOutputExpectation(
                n_w=3,
                preprocessing_function=WeightedMCMultiOutputObjective(
                    torch.tensor([-1.0, -1.0])
                )
                if use_preprocessing
                else None,
            )
            kwargs = c(
                model=mm,
                training_data=self.blockX_blockY,
                objective_thresholds=objective_thresholds,
                objective=obj,
            )
            expected_obj_t = (
                -objective_thresholds if use_preprocessing else objective_thresholds
            )
            self.assertIs(kwargs["objective"], obj)
            self.assertTrue(torch.equal(kwargs["ref_point"], expected_obj_t))
            partitioning = kwargs["partitioning"]
            self.assertIsInstance(partitioning, FastNondominatedPartitioning)
            self.assertTrue(torch.equal(partitioning.ref_point, expected_obj_t))

    def test_construct_inputs_qEHVI(self) -> None:
        c = get_acqf_input_constructor(qExpectedHypervolumeImprovement)
        objective_thresholds = torch.rand(2)

        # Test defaults
        mm = SingleTaskGP(torch.rand(1, 2), torch.rand(1, 2))
        mean = mm.posterior(self.blockX_blockY[0].X()).mean
        kwargs = c(
            model=mm,
            training_data=self.blockX_blockY,
            objective_thresholds=objective_thresholds,
        )
        self.assertIsInstance(kwargs["objective"], IdentityMCMultiOutputObjective)
        ref_point_expected = objective_thresholds
        self.assertTrue(torch.equal(kwargs["ref_point"], ref_point_expected))
        partitioning = kwargs["partitioning"]
        self.assertIsInstance(partitioning, FastNondominatedPartitioning)
        self.assertTrue(torch.equal(partitioning.ref_point, ref_point_expected))
        self.assertTrue(torch.equal(partitioning._neg_Y, -mean))
        sampler = kwargs["sampler"]
        self.assertIsInstance(sampler, SobolQMCNormalSampler)
        self.assertEqual(sampler.sample_shape, torch.Size([128]))
        self.assertIsNone(kwargs["X_pending"])
        self.assertIsNone(kwargs["constraints"])
        self.assertEqual(kwargs["eta"], 1e-3)

        # Test IID sampler
        kwargs = c(
            model=mm,
            training_data=self.blockX_blockY,
            objective_thresholds=objective_thresholds,
            qmc=False,
            mc_samples=64,
        )
        sampler = kwargs["sampler"]
        self.assertIsInstance(sampler, IIDNormalSampler)
        self.assertEqual(sampler.sample_shape, torch.Size([64]))

        # Test outcome constraints and custom inputs
        mean = torch.tensor([[1.0, 0.25], [0.5, 1.0]])
        variance = torch.ones(1, 1)
        mm = MockModel(MockPosterior(mean=mean, variance=variance))
        weights = torch.rand(2)
        obj = WeightedMCMultiOutputObjective(weights=weights)
        outcome_constraints = (torch.tensor([[0.0, 1.0]]), torch.tensor([[0.5]]))
        constraints = get_outcome_constraint_transforms(
            outcome_constraints=outcome_constraints
        )
        X_pending = torch.rand(1, 2)
        kwargs = c(
            model=mm,
            training_data=self.blockX_blockY,
            objective_thresholds=objective_thresholds,
            objective=obj,
            constraints=constraints,
            X_pending=X_pending,
            alpha=0.05,
            eta=1e-2,
        )
        self.assertIsInstance(kwargs["objective"], WeightedMCMultiOutputObjective)
        ref_point_expected = objective_thresholds * weights
        self.assertTrue(torch.equal(kwargs["ref_point"], ref_point_expected))
        partitioning = kwargs["partitioning"]
        self.assertIsInstance(partitioning, NondominatedPartitioning)
        self.assertEqual(partitioning.alpha, 0.05)
        self.assertTrue(torch.equal(partitioning._neg_ref_point, -ref_point_expected))
        Y_expected = mean[:1] * weights
        self.assertTrue(torch.equal(partitioning._neg_Y, -Y_expected))
        self.assertTrue(torch.equal(kwargs["X_pending"], X_pending))
        self.assertIs(kwargs["constraints"], constraints)
        self.assertEqual(kwargs["eta"], 1e-2)

        # Test check for block designs
        with self.assertRaisesRegex(ValueError, "Field `X` must be shared"):
            c(
                model=mm,
                training_data=self.multiX_multiY,
                objective_thresholds=objective_thresholds,
                objective=obj,
                constraints=constraints,
                X_pending=X_pending,
                alpha=0.05,
                eta=1e-2,
            )

        # Test custom sampler
        custom_sampler = SobolQMCNormalSampler(sample_shape=torch.Size([16]), seed=1234)
        kwargs = c(
            model=mm,
            training_data=self.blockX_blockY,
            objective_thresholds=objective_thresholds,
            sampler=custom_sampler,
        )
        sampler = kwargs["sampler"]
        self.assertIsInstance(sampler, SobolQMCNormalSampler)
        self.assertEqual(sampler.sample_shape, torch.Size([16]))
        self.assertEqual(sampler.seed, 1234)

    def test_construct_inputs_qNEHVI(self) -> None:
        c = get_acqf_input_constructor(qNoisyExpectedHypervolumeImprovement)
        objective_thresholds = torch.rand(2)

        # Test defaults
        kwargs = c(
            model=SingleTaskGP(torch.rand(1, 2), torch.rand(1, 2)),
            training_data=self.blockX_blockY,
            objective_thresholds=objective_thresholds,
        )
        ref_point_expected = objective_thresholds
        self.assertTrue(torch.equal(kwargs["ref_point"], ref_point_expected))
        self.assertTrue(torch.equal(kwargs["X_baseline"], self.blockX_blockY[0].X()))
        self.assertIsInstance(kwargs["sampler"], SobolQMCNormalSampler)
        self.assertEqual(kwargs["sampler"].sample_shape, torch.Size([128]))
        self.assertIsInstance(kwargs["objective"], IdentityMCMultiOutputObjective)
        self.assertIsNone(kwargs["constraints"])
        self.assertIsNone(kwargs["X_pending"])
        self.assertEqual(kwargs["eta"], 1e-3)
        self.assertTrue(kwargs["prune_baseline"])
        self.assertEqual(kwargs["alpha"], 0.0)
        self.assertTrue(kwargs["cache_pending"])
        self.assertEqual(kwargs["max_iep"], 0)
        self.assertTrue(kwargs["incremental_nehvi"])
        self.assertTrue(kwargs["cache_root"])

        # Test check for block designs
        mock_model = mock.Mock()
        mock_model.num_outputs = 2
        with self.assertRaisesRegex(ValueError, "Field `X` must be shared"):
            c(
                model=mock_model,
                training_data=self.multiX_multiY,
                objective_thresholds=objective_thresholds,
            )

        # Test custom inputs
        weights = torch.rand(2)
        objective = WeightedMCMultiOutputObjective(weights=weights)
        X_baseline = torch.rand(2, 2)
        sampler = IIDNormalSampler(sample_shape=torch.Size([4]))
        outcome_constraints = (torch.tensor([[0.0, 1.0]]), torch.tensor([[0.5]]))
        constraints = get_outcome_constraint_transforms(
            outcome_constraints=outcome_constraints
        )
        X_pending = torch.rand(1, 2)
        kwargs = c(
            model=mock_model,
            training_data=self.blockX_blockY,
            objective_thresholds=objective_thresholds,
            objective=objective,
            X_baseline=X_baseline,
            sampler=sampler,
            constraints=constraints,
            X_pending=X_pending,
            eta=1e-2,
            prune_baseline=True,
            alpha=0.0,
            cache_pending=False,
            max_iep=1,
            incremental_nehvi=False,
            cache_root=False,
        )
        ref_point_expected = objective(objective_thresholds)
        self.assertTrue(torch.equal(kwargs["ref_point"], ref_point_expected))
        self.assertTrue(torch.equal(kwargs["X_baseline"], X_baseline))
        sampler_ = kwargs["sampler"]
        self.assertIsInstance(sampler_, IIDNormalSampler)
        self.assertEqual(sampler_.sample_shape, torch.Size([4]))
        self.assertEqual(kwargs["objective"], objective)
        self.assertIs(kwargs["constraints"], constraints)
        self.assertTrue(torch.equal(kwargs["X_pending"], X_pending))
        self.assertEqual(kwargs["eta"], 1e-2)
        self.assertTrue(kwargs["prune_baseline"])
        self.assertEqual(kwargs["alpha"], 0.0)
        self.assertFalse(kwargs["cache_pending"])
        self.assertEqual(kwargs["max_iep"], 1)
        self.assertFalse(kwargs["incremental_nehvi"])
        self.assertFalse(kwargs["cache_root"])

        # Test with risk measures.
        with self.assertRaisesRegex(UnsupportedError, "feasibility-weighted"):
            kwargs = c(
                model=mock_model,
                training_data=self.blockX_blockY,
                objective_thresholds=objective_thresholds,
                objective=MultiOutputExpectation(n_w=3),
                constraints=constraints,
            )
        for use_preprocessing in (True, False):
            obj = MultiOutputExpectation(
                n_w=3,
                preprocessing_function=WeightedMCMultiOutputObjective(
                    torch.tensor([-1.0, -1.0])
                )
                if use_preprocessing
                else None,
            )
            kwargs = c(
                model=mock_model,
                training_data=self.blockX_blockY,
                objective_thresholds=objective_thresholds,
                objective=obj,
            )
            expected_obj_t = (
                -objective_thresholds if use_preprocessing else objective_thresholds
            )
            self.assertIs(kwargs["objective"], obj)
            self.assertTrue(torch.equal(kwargs["ref_point"], expected_obj_t))

        # Test default alpha for many objectives/
        mock_model.num_outputs = 5
        kwargs = c(
            model=mock_model,
            training_data=self.blockX_blockY,
            objective_thresholds=objective_thresholds,
        )
        self.assertEqual(kwargs["alpha"], 0.0)

    def test_construct_inputs_kg(self) -> None:
        current_value = torch.tensor(1.23)
        with mock.patch(
            target="botorch.acquisition.input_constructors.optimize_objective",
            return_value=(None, current_value),
        ):
            from botorch.acquisition import input_constructors

            func = input_constructors.get_acqf_input_constructor(qKnowledgeGradient)
            kwargs = func(
                model=mock.Mock(),
                training_data=self.blockX_blockY,
                objective=LinearMCObjective(torch.rand(2)),
                bounds=self.bounds,
                num_fantasies=33,
            )

            self.assertEqual(kwargs["num_fantasies"], 33)
            self.assertEqual(kwargs["current_value"], current_value)

    def test_construct_inputs_mes(self) -> None:
        func = get_acqf_input_constructor(qMaxValueEntropy)
        model = SingleTaskGP(train_X=torch.ones((3, 2)), train_Y=torch.zeros((3, 1)))
        kwargs = func(
            model=model,
            training_data=self.blockX_blockY,
            objective=LinearMCObjective(torch.rand(2)),
            bounds=self.bounds,
            candidate_size=17,
            maximize=False,
        )

        self.assertFalse(kwargs["maximize"])
        self.assertGreaterEqual(kwargs["candidate_set"].min(), 0.0)
        self.assertLessEqual(kwargs["candidate_set"].max(), 1.0)
        self.assertEqual(
            [int(s) for s in kwargs["candidate_set"].shape], [17, len(self.bounds)]
        )

        acqf = qMaxValueEntropy(**kwargs)
        self.assertIs(acqf.model, model)

    def test_construct_inputs_mf_base(self) -> None:
        target_fidelities = {0: 0.123}
        fidelity_weights = {0: 0.456}
        cost_intercept = 0.789
        num_trace_observations = 0

        with self.subTest("test_fully_specified"):
            kwargs = construct_inputs_mf_base(
                target_fidelities=target_fidelities,
                fidelity_weights=fidelity_weights,
                cost_intercept=cost_intercept,
                num_trace_observations=num_trace_observations,
            )

            X = torch.rand(3, 2)
            self.assertIsInstance(kwargs["expand"], Callable)
            self.assertTrue(
                torch.equal(
                    kwargs["expand"](X),
                    expand_trace_observations(
                        X=X,
                        fidelity_dims=sorted(target_fidelities),
                        num_trace_obs=num_trace_observations,
                    ),
                )
            )

            self.assertIsInstance(kwargs["project"], Callable)
            self.assertTrue(
                torch.equal(
                    kwargs["project"](X),
                    project_to_target_fidelity(X, target_fidelities=target_fidelities),
                )
            )

            cm = kwargs["cost_aware_utility"].cost_model
            w = torch.tensor(list(fidelity_weights.values()), dtype=cm.weights.dtype)
            self.assertEqual(cm.fixed_cost, cost_intercept)
            self.assertAllClose(cm.weights, w)

        with self.subTest("test_missing_fidelity_weights"):
            kwargs = construct_inputs_mf_base(
                target_fidelities=target_fidelities,
                cost_intercept=cost_intercept,
            )
            cm = kwargs["cost_aware_utility"].cost_model
            self.assertAllClose(cm.weights, torch.ones_like(cm.weights))

        with self.subTest("test_mismatched_weights"):
            with self.assertRaisesRegex(
                RuntimeError, "Must provide the same indices for"
            ):
                construct_inputs_mf_base(
                    target_fidelities={0: 1.0},
                    fidelity_weights={1: 0.5},
                    cost_intercept=cost_intercept,
                )

    def test_construct_inputs_mfkg(self) -> None:
        constructor_args = {
            "model": None,
            "training_data": self.blockX_blockY,
            "objective": None,
            "bounds": self.bounds,
            "num_fantasies": 123,
            "target_fidelities": {0: 0.987},
            "fidelity_weights": {0: 0.654},
            "cost_intercept": 0.321,
        }
        with mock.patch(
            target="botorch.acquisition.input_constructors.construct_inputs_mf_base",
            return_value={"foo": 0},
        ), mock.patch(
            target="botorch.acquisition.input_constructors.construct_inputs_qKG",
            return_value={"bar": 1},
        ):
            from botorch.acquisition import input_constructors

            input_constructor = input_constructors.get_acqf_input_constructor(
                qMultiFidelityKnowledgeGradient
            )
            inputs_mfkg = input_constructor(**constructor_args)
            inputs_test = {"foo": 0, "bar": 1}
            self.assertEqual(inputs_mfkg, inputs_test)

    def test_construct_inputs_mfmes(self) -> None:
        target_fidelities = {0: 0.987}
        constructor_args = {
            "model": None,
            "training_data": self.blockX_blockY,
            "objective": None,
            "bounds": self.bounds,
            "num_fantasies": 123,
            "candidate_size": 17,
            "target_fidelities": target_fidelities,
            "fidelity_weights": {0: 0.654},
            "cost_intercept": 0.321,
        }
        current_value = torch.tensor(1.23)
        with mock.patch(
            target="botorch.acquisition.input_constructors.construct_inputs_mf_base",
            return_value={"foo": 0},
        ), mock.patch(
            target="botorch.acquisition.input_constructors.construct_inputs_qMES",
            return_value={"bar": 1},
        ), mock.patch(
            target="botorch.acquisition.input_constructors.optimize_objective",
            return_value=(None, current_value),
        ):
            from botorch.acquisition import input_constructors

            input_constructor = input_constructors.get_acqf_input_constructor(
                qMultiFidelityMaxValueEntropy
            )
            inputs_mfmes = input_constructor(**constructor_args)
            inputs_test = {
                "foo": 0,
                "bar": 1,
                "current_value": current_value,
                "target_fidelities": target_fidelities,
            }
            self.assertEqual(inputs_mfmes, inputs_test)

    def test_construct_inputs_jes(self) -> None:
        func = get_acqf_input_constructor(qJointEntropySearch)
        # we need to run optimize_posterior_samples, so we sort of need
        # a real model as there is no other (apparent) option
        model = SingleTaskGP(self.blockX_blockY[0].X(), self.blockX_blockY[0].Y())

        kwargs = func(
            model=model,
            training_data=self.blockX_blockY,
            objective=LinearMCObjective(torch.rand(2)),
            bounds=self.bounds,
            num_optima=17,
            maximize=False,
        )

        self.assertFalse(kwargs["maximize"])
        self.assertEqual(
            self.blockX_blockY[0].X().dtype, kwargs["optimal_inputs"].dtype
        )
        self.assertEqual(len(kwargs["optimal_inputs"]), 17)
        self.assertEqual(len(kwargs["optimal_outputs"]), 17)
        # asserting that, for the non-batch case, the optimal inputs are
        # of shape N x D and outputs are N x 1
        self.assertEqual(len(kwargs["optimal_inputs"].shape), 2)
        self.assertEqual(len(kwargs["optimal_outputs"].shape), 2)
        qJointEntropySearch(**kwargs)


class TestInstantiationFromInputConstructor(
    InputConstructorBaseTestCase, BotorchTestCase
):
    def _test_constructor_base(
        self, classes: List[Type[AcquisitionFunction]], **input_constructor_kwargs: Any
    ) -> None:
        for cls_ in classes:
            with self.subTest(cls_.__name__, cls_=cls_):
                acqf_kwargs = get_acqf_input_constructor(cls_)(
                    **input_constructor_kwargs
                )
                # no assertions; we are just testing that this doesn't error
                cls_(**acqf_kwargs)

    def test_constructors_like_PosteriorMean(self) -> None:
        classes = [PosteriorMean, UpperConfidenceBound, qUpperConfidenceBound]
        self._test_constructor_base(classes=classes, model=self.mock_model)

    def test_constructors_like_ExpectedImprovement(self) -> None:
        classes = [
            ExpectedImprovement,
            LogExpectedImprovement,
            ProbabilityOfImprovement,
            LogProbabilityOfImprovement,
            NoisyExpectedImprovement,
            LogNoisyExpectedImprovement,
            qExpectedImprovement,
            qLogExpectedImprovement,
            qNoisyExpectedImprovement,
            qLogNoisyExpectedImprovement,
            qProbabilityOfImprovement,
        ]
        model = FixedNoiseGP(
            train_X=torch.rand((4, 2)),
            train_Y=torch.rand((4, 1)),
            train_Yvar=torch.ones((4, 1)),
        )
        self._test_constructor_base(
            classes=classes, model=model, training_data=self.blockX_blockY
        )

    def test_constructors_like_qNEHVI(self) -> None:
        objective_thresholds = torch.tensor([0.1, 0.2])
        model = SingleTaskGP(train_X=torch.rand((3, 2)), train_Y=torch.rand((3, 2)))
        # The EHVI and qEHVI input constructors are not working
        classes = [
            qNoisyExpectedHypervolumeImprovement,
            # ExpectedHypervolumeImprovement,
            # qExpectedHypervolumeImprovement,
        ]
        self._test_constructor_base(
            classes=classes,
            model=model,
            training_data=self.blockX_blockY,
            objective_thresholds=objective_thresholds,
        )

    def test_constructors_like_qMaxValueEntropy(self) -> None:
        bounds = torch.ones((1, 2))
        classes = [qMaxValueEntropy, qKnowledgeGradient]
        self._test_constructor_base(
            classes=classes,
            model=SingleTaskGP(train_X=torch.rand((3, 1)), train_Y=torch.rand((3, 1))),
            training_data=self.blockX_blockY,
            bounds=bounds,
        )

    def test_constructors_like_qMultiFidelityKnowledgeGradient(self) -> None:
        classes = [
            qMultiFidelityKnowledgeGradient,
            # currently the input constructor for qMFMVG is not working
            # qMultiFidelityMaxValueEntropy
        ]
        self._test_constructor_base(
            classes=classes,
            model=SingleTaskGP(train_X=torch.rand((3, 1)), train_Y=torch.rand((3, 1))),
            training_data=self.blockX_blockY,
            bounds=torch.ones((1, 2)),
            target_fidelities={0: 0.987},
        )

    def test_eubo(self) -> None:
        model = SingleTaskGP(train_X=torch.rand((3, 2)), train_Y=torch.rand((3, 2)))
        pref_model = self.mock_model
        pref_model.dim = 2
        pref_model.datapoints = torch.tensor([])

        classes = [AnalyticExpectedUtilityOfBestOption]
        self._test_constructor_base(
            classes=classes,
            model=model,
            pref_model=pref_model,
        )

    def test_qjes(self) -> None:
        model = SingleTaskGP(self.blockX_blockY[0].X(), self.blockX_blockY[0].Y())
        self._test_constructor_base(
            classes=[qJointEntropySearch],
            model=model,
            bounds=self.bounds,
        )
