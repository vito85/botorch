#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

r"""
Utilities for acquisition functions.
"""

from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional, Tuple

import torch
from botorch.acquisition.objective import (
    IdentityMCObjective,
    MCAcquisitionObjective,
    PosteriorTransform,
)
from botorch.exceptions.errors import DeprecationError, UnsupportedError
from botorch.models.fully_bayesian import MCMC_DIM
from botorch.models.model import Model
from botorch.sampling.base import MCSampler
from botorch.sampling.get_sampler import get_sampler
from botorch.sampling.pathwise import draw_matheron_paths
from botorch.utils.objective import compute_feasibility_indicator
from botorch.utils.sampling import optimize_posterior_samples
from botorch.utils.transforms import is_fully_bayesian
from torch import Tensor


def get_acquisition_function(*args, **kwargs) -> None:
    raise DeprecationError(
        "`get_acquisition_function` has been moved to `botorch.acquisition.factory`."
    )


def compute_best_feasible_objective(
    samples: Tensor,
    obj: Tensor,
    constraints: Optional[List[Callable[[Tensor], Tensor]]],
    model: Optional[Model] = None,
    objective: Optional[MCAcquisitionObjective] = None,
    posterior_transform: Optional[PosteriorTransform] = None,
    X_baseline: Optional[Tensor] = None,
    infeasible_obj: Optional[Tensor] = None,
) -> Tensor:
    """Computes the largest `obj` value that is feasible under the `constraints`. If
    `constraints` is None, returns the best unconstrained objective value.

    When no feasible observations exist and `infeasible_obj` is not `None`, returns
    `infeasible_obj` (potentially reshaped). When no feasible observations exist and
    `infeasible_obj` is `None`, uses `model`, `objective`, `posterior_transform`, and
    `X_baseline` to infer and return an `infeasible_obj` `M` s.t. `M < min_x f(x)`.

    Args:
        samples: `(sample_shape) x batch_shape x q x m`-dim posterior samples.
        obj: A `(sample_shape) x batch_shape x q`-dim Tensor of MC objective values.
        constraints: A list of constraint callables which map posterior samples to
            a scalar. The associated constraint is considered satisfied if this
            scalar is less than zero.
        model: A Model, only required when there are no feasible observations.
        objective: An MCAcquisitionObjective, only optionally used when there are no
            feasible observations.
        posterior_transform: A PosteriorTransform, only optionally used when there are
            no feasible observations.
        X_baseline: A `batch_shape x d`-dim Tensor of baseline points, only required
            when there are no feasible observations.
        infeasible_obj: A Tensor to be returned when no feasible points exist.

    Returns:
        A `(sample_shape) x batch_shape x 1`-dim Tensor of best feasible objectives.
    """
    if constraints is None:  # unconstrained case
        # we don't need to differentiate through X_baseline for now, so taking
        # the regular max over the n points to get best_f is fine
        with torch.no_grad():
            return obj.amax(dim=-1, keepdim=True)

    is_feasible = compute_feasibility_indicator(
        constraints=constraints, samples=samples
    )  # sample_shape x batch_shape x q

    if is_feasible.any(dim=-1).all():
        infeasible_value = -torch.inf

    elif infeasible_obj is not None:
        infeasible_value = infeasible_obj.item()

    else:
        if model is None:
            raise ValueError(
                "Must specify `model` when no feasible observation exists."
            )
        if X_baseline is None:
            raise ValueError(
                "Must specify `X_baseline` when no feasible observation exists."
            )
        infeasible_value = _estimate_objective_lower_bound(
            model=model,
            objective=objective,
            posterior_transform=posterior_transform,
            X=X_baseline,
        ).item()

    obj = torch.where(is_feasible, obj, infeasible_value)
    with torch.no_grad():
        return obj.amax(dim=-1, keepdim=True)


def _estimate_objective_lower_bound(
    model: Model,
    objective: Optional[MCAcquisitionObjective],
    posterior_transform: Optional[PosteriorTransform],
    X: Tensor,
) -> Tensor:
    """Estimates a lower bound on the objective values by evaluating the model at convex
    combinations of `X`, returning the 6-sigma lower bound of the computed statistics.

    Args:
        model: A fitted model.
        objective: An MCAcquisitionObjective with `m` outputs.
        posterior_transform: A PosteriorTransform.
        X: A `n x d`-dim Tensor of design points from which to draw convex combinations.

    Returns:
        A `m`-dimensional Tensor of lower bounds of the objectives.
    """
    convex_weights = torch.rand(
        32,
        X.shape[-2],
        dtype=X.dtype,
        device=X.device,
    )
    weights_sum = convex_weights.sum(dim=0, keepdim=True)
    convex_weights = convex_weights / weights_sum
    # infeasible cost M is such that -M < min_x f(x), thus
    # 0 < min_x f(x) - (-M), so we should take -M as a lower
    # bound on the best feasible objective
    return -get_infeasible_cost(
        X=convex_weights @ X,
        model=model,
        objective=objective,
        posterior_transform=posterior_transform,
    )


def get_infeasible_cost(
    X: Tensor,
    model: Model,
    objective: Optional[Callable[[Tensor, Optional[Tensor]], Tensor]] = None,
    posterior_transform: Optional[PosteriorTransform] = None,
) -> Tensor:
    r"""Get infeasible cost for a model and objective.

    For each outcome, computes an infeasible cost `M` such that
    `-M < min_x f(x)` almost always, so that feasible points are preferred.

    Args:
        X: A `n x d` Tensor of `n` design points to use in evaluating the
            minimum. These points should cover the design space well. The more
            points the better the estimate, at the expense of added computation.
        model: A fitted botorch model with `m` outcomes.
        objective: The objective with which to evaluate the model output.
        posterior_transform: A PosteriorTransform (optional).

    Returns:
        An `m`-dim tensor of infeasible cost values.

    Example:
        >>> model = SingleTaskGP(train_X, train_Y)
        >>> objective = lambda Y: Y[..., -1] ** 2
        >>> M = get_infeasible_cost(train_X, model, obj)
    """
    if objective is None:

        def objective(Y: Tensor, X: Optional[Tensor] = None):
            return Y.squeeze(-1)

    posterior = model.posterior(X, posterior_transform=posterior_transform)
    lb = objective(posterior.mean - 6 * posterior.variance.clamp_min(0).sqrt(), X=X)
    if lb.ndim < posterior.mean.ndim:
        lb = lb.unsqueeze(-1)
    # Take outcome-wise min. Looping in to handle batched models.
    while lb.dim() > 1:
        lb = lb.min(dim=-2).values
    return -(lb.clamp_max(0.0))


def prune_inferior_points(
    model: Model,
    X: Tensor,
    objective: Optional[MCAcquisitionObjective] = None,
    posterior_transform: Optional[PosteriorTransform] = None,
    num_samples: int = 2048,
    max_frac: float = 1.0,
    sampler: Optional[MCSampler] = None,
    marginalize_dim: Optional[int] = None,
) -> Tensor:
    r"""Prune points from an input tensor that are unlikely to be the best point.

    Given a model, an objective, and an input tensor `X`, this function returns
    the subset of points in `X` that have some probability of being the best
    point under the objective. This function uses sampling to estimate the
    probabilities, the higher the number of points `n` in `X` the higher the
    number of samples `num_samples` should be to obtain accurate estimates.

    Args:
        model: A fitted model. Batched models are currently not supported.
        X: An input tensor of shape `n x d`. Batched inputs are currently not
            supported.
        objective: The objective under which to evaluate the posterior.
        posterior_transform: A PosteriorTransform (optional).
        num_samples: The number of samples used to compute empirical
            probabilities of being the best point.
        max_frac: The maximum fraction of points to retain. Must satisfy
            `0 < max_frac <= 1`. Ensures that the number of elements in the
            returned tensor does not exceed `ceil(max_frac * n)`.
        sampler: If provided, will use this customized sampler instead of
            automatically constructing one with `num_samples`.
        marginalize_dim: A batch dimension that should be marginalized.
            For example, this is useful when using a batched fully Bayesian
            model.

    Returns:
        A `n' x d` with subset of points in `X`, where

            n' = min(N_nz, ceil(max_frac * n))

        with `N_nz` the number of points in `X` that have non-zero (empirical,
        under `num_samples` samples) probability of being the best point.
    """
    if marginalize_dim is None and is_fully_bayesian(model):
        # TODO: Properly deal with marginalizing fully Bayesian models
        marginalize_dim = MCMC_DIM

    if X.ndim > 2:
        # TODO: support batched inputs (req. dealing with ragged tensors)
        raise UnsupportedError(
            "Batched inputs `X` are currently unsupported by prune_inferior_points"
        )
    max_points = math.ceil(max_frac * X.size(-2))
    if max_points < 1 or max_points > X.size(-2):
        raise ValueError(f"max_frac must take values in (0, 1], is {max_frac}")
    with torch.no_grad():
        posterior = model.posterior(X=X, posterior_transform=posterior_transform)
    if sampler is None:
        sampler = get_sampler(
            posterior=posterior, sample_shape=torch.Size([num_samples])
        )
    samples = sampler(posterior)
    if objective is None:
        objective = IdentityMCObjective()
    obj_vals = objective(samples, X=X)
    if obj_vals.ndim > 2:
        if obj_vals.ndim == 3 and marginalize_dim is not None:
            obj_vals = obj_vals.mean(dim=marginalize_dim)
        else:
            # TODO: support batched inputs (req. dealing with ragged tensors)
            raise UnsupportedError(
                "Models with multiple batch dims are currently unsupported by"
                " prune_inferior_points."
            )
    is_best = torch.argmax(obj_vals, dim=-1)
    idcs, counts = torch.unique(is_best, return_counts=True)

    if len(idcs) > max_points:
        counts, order_idcs = torch.sort(counts, descending=True)
        idcs = order_idcs[:max_points]

    return X[idcs]


def project_to_target_fidelity(
    X: Tensor, target_fidelities: Optional[Dict[int, float]] = None
) -> Tensor:
    r"""Project `X` onto the target set of fidelities.

    This function assumes that the set of feasible fidelities is a box, so
    projecting here just means setting each fidelity parameter to its target
    value.

    Args:
        X: A `batch_shape x q x d`-dim Tensor of with `q` `d`-dim design points
            for each t-batch.
        target_fidelities: A dictionary mapping a subset of columns of `X` (the
            fidelity parameters) to their respective target fidelity value. If
            omitted, assumes that the last column of X is the fidelity parameter
            with a target value of 1.0.

    Return:
        A `batch_shape x q x d`-dim Tensor `X_proj` with fidelity parameters
            projected to the provided fidelity values.
    """
    if target_fidelities is None:
        target_fidelities = {-1: 1.0}
    d = X.size(-1)
    # normalize to positive indices
    tfs = {k if k >= 0 else d + k: v for k, v in target_fidelities.items()}
    ones = torch.ones(*X.shape[:-1], device=X.device, dtype=X.dtype)
    # here we're looping through the feature dimension of X - this could be
    # slow for large `d`, we should optimize this for that case
    X_proj = torch.stack(
        [X[..., i] if i not in tfs else tfs[i] * ones for i in range(d)], dim=-1
    )
    return X_proj


def expand_trace_observations(
    X: Tensor, fidelity_dims: Optional[List[int]] = None, num_trace_obs: int = 0
) -> Tensor:
    r"""Expand `X` with trace observations.

    Expand a tensor of inputs with "trace observations" that are obtained during
    the evaluation of the candidate set. This is used in multi-fidelity
    optimization. It can be though of as augmenting the `q`-batch with additional
    points that are the expected trace observations.

    Let `f_i` be the `i`-th fidelity parameter. Then this functions assumes that
    for each element of the q-batch, besides the fidelity `f_i`, we will observe
    additonal fidelities `f_i1, ..., f_iK`, where `K = num_trace_obs`, during
    evaluation of the candidate set `X`. Specifically, this function assumes
    that `f_ij = (K-j) / (num_trace_obs + 1) * f_i` for all `i`. That is, the
    expansion is performed in parallel for all fidelities (it does not expand
    out all possible combinations).

    Args:
        X: A `batch_shape x q x d`-dim Tensor of with `q` `d`-dim design points
            (incl. the fidelity parameters) for each t-batch.
        fidelity_dims: The indices of the fidelity parameters. If omitted,
            assumes that the last column of X contains the fidelity parameters.
        num_trace_obs: The number of trace observations to use.

    Return:
        A `batch_shape x (q + num_trace_obs x q) x d` Tensor `X_expanded` that
            expands `X` with trace observations.
    """
    if num_trace_obs == 0:  # No need to expand if we don't use trace observations
        return X

    if fidelity_dims is None:
        fidelity_dims = [-1]

    # The general strategy in the following is to expand `X` to the desired
    # shape, and then multiply it (point-wise) with a tensor of scaling factors
    reps = [1] * (X.ndim - 2) + [1 + num_trace_obs, 1]
    X_expanded = X.repeat(*reps)  # batch_shape x (q + num_trace_obs x q) x d
    scale_fac = torch.ones_like(X_expanded)
    s_pad = 1 / (num_trace_obs + 1)
    # tensor of  num_trace_obs scaling factors equally space between 1-s_pad and s_pad
    sf = torch.linspace(1 - s_pad, s_pad, num_trace_obs, device=X.device, dtype=X.dtype)
    # repeat each element q times
    q = X.size(-2)
    sf = torch.repeat_interleave(sf, q)  # num_trace_obs * q
    # now expand this to num_trace_obs x q x num_fidelities
    sf = sf.unsqueeze(-1).expand(X_expanded.size(-2) - q, len(fidelity_dims))
    # change relevant entries of the scaling tensor
    scale_fac[..., q:, fidelity_dims] = sf
    return scale_fac * X_expanded


def project_to_sample_points(X: Tensor, sample_points: Tensor) -> Tensor:
    r"""Augment `X` with sample points at which to take weighted average.

    Args:
        X: A `batch_shape x 1 x d`-dim Tensor of with one d`-dim design points
            for each t-batch.
        sample_points: `p x d'`-dim Tensor (`d' < d`) of `d'`-dim sample points at
            which to compute the expectation. The `d'`-dims refer to the trailing
            columns of X.
    Returns:
        A `batch_shape x p x d` Tensor where the q-batch includes the `p` sample points.
    """
    batch_shape = X.shape[:-2]
    p, d_prime = sample_points.shape
    X_new = X.repeat(*(1 for _ in batch_shape), p, 1)  # batch_shape x p x d
    X_new[..., -d_prime:] = sample_points
    return X_new


def get_optimal_samples(
    model: Model,
    bounds: Tensor,
    num_optima: int,
    raw_samples: int = 1024,
    num_restarts: int = 20,
    maximize: bool = True,
) -> Tuple[Tensor, Tensor]:
    """Draws sample paths from the posterior and maximizes the samples using GD.

    Args:
        model (Model): The model from which samples are drawn.
        bounds: (Tensor): Bounds of the search space. If the model inputs are
            normalized, the bounds should be normalized as well.
        num_optima (int): The number of paths to be drawn and optimized.
        raw_samples (int, optional): The number of candidates randomly sample.
            Defaults to 1024.
        num_restarts (int, optional): The number of candidates to do gradient-based
            optimization on. Defaults to 20.
        maximize: Whether to maximize or minimize the samples.
    Returns:
        Tuple[Tensor, Tensor]: The optimal input locations and corresponding
        outputs, x* and f*.

    """
    paths = draw_matheron_paths(model, sample_shape=torch.Size([num_optima]))
    optimal_inputs, optimal_outputs = optimize_posterior_samples(
        paths,
        bounds=bounds,
        raw_samples=raw_samples,
        num_restarts=num_restarts,
        maximize=maximize,
    )
    return optimal_inputs, optimal_outputs
