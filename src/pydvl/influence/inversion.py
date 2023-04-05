"""
Contains methods to invert the hessian vector product. 
"""
import logging
from enum import Enum
from typing import Any, Dict

from .frameworks import (
    ModelType,
    TensorType,
    TwiceDifferentiable,
    solve_batch_cg,
    solve_linear,
    solve_lissa,
)

__all__ = ["solve_hvp"]

logger = logging.getLogger(__name__)


class InversionMethod(str, Enum):
    """
    Different inversion methods types.
    """

    Direct = "direct"
    Cg = "cg"
    Lissa = "lissa"


def solve_hvp(
    inversion_method: InversionMethod,
    model: TwiceDifferentiable[TensorType, ModelType],
    x: TensorType,
    y: TensorType,
    b: TensorType,
    lam: float = 0,
    inversion_method_kwargs: Dict[str, Any] = {},
    progress: bool = False,
) -> TensorType:
    """
    Finds $x$ such that $Ax = b$, where $A$ is the hessian of model,
    and $b$ a vector.
    Depending on the inversion method, the hessian is either calculated directly
    and then inverted, or implicitly and then inverted through matrix vector
    product. The method also allows to add a small regularization term (lam)
    to facilitate inversion of non fully trained models.

    :param inversion_method:
    :param model: A model wrapped in the TwiceDifferentiable interface.
    :param x: An array containing the features of the input data points.
    :param y: labels for x
    :param b:
    :param lam: regularization of the hessian
    :param inversion_method_kwargs: kwargs to pass to the inversion method
    :param progress: If True, display progress bars.

    :return: An array that solves the inverse problem,
        i.e. it returns $x$ such that $Ax = b$
    """
    if inversion_method == InversionMethod.Direct:
        return solve_linear(
            model,
            x,
            y,
            b,
            lam,
            **inversion_method_kwargs,
            progress=progress,
        )
    elif inversion_method == InversionMethod.Cg:
        return solve_batch_cg(
            model,
            x,
            y,
            b,
            lam,
            **inversion_method_kwargs,
            progress=progress,
        )
    elif inversion_method == InversionMethod.Lissa:
        return solve_lissa(
            model,
            x,
            y,
            b,
            lam,
            **inversion_method_kwargs,
            progress=progress,
        )
    else:
        raise ValueError(f"Unknown inversion method: {inversion_method}")
