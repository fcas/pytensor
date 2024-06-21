from collections.abc import Sequence
from copy import copy
from typing import Any, cast

import numpy as np

from pytensor import config
from pytensor.compile.builders import OpFromGraph
from pytensor.gradient import DisconnectedType
from pytensor.graph.basic import Apply, Constant
from pytensor.graph.null_type import NullType
from pytensor.graph.op import Op
from pytensor.graph.replace import (
    _vectorize_node,
    _vectorize_not_needed,
    vectorize_graph,
)
from pytensor.scalar import ScalarType
from pytensor.tensor import as_tensor_variable
from pytensor.tensor.shape import shape_padleft
from pytensor.tensor.type import TensorType, continuous_dtypes, discrete_dtypes, tensor
from pytensor.tensor.utils import (
    _parse_gufunc_signature,
    broadcast_static_dim_lengths,
    import_func_from_string,
    safe_signature,
)
from pytensor.tensor.variable import TensorVariable


class Blockwise(Op):
    """Generalizes a core `Op` to work with batched dimensions.

    TODO: Dispatch JAX (should be easy with the vectorize macro)
    TODO: Dispatch Numba
    TODO: C implementation?
    TODO: Fuse Blockwise?
    """

    __props__ = ("core_op", "signature")

    def __init__(
        self,
        core_op: Op,
        signature: str | None = None,
        name: str | None = None,
        gufunc_spec: tuple[str, int, int] | None = None,
        **kwargs,
    ):
        """

        Parameters
        ----------
        core_op
            An instance of a subclass of `Op` which works on the core case.
        signature
            Generalized universal function signature,
            e.g., (m,n),(n)->(m) for vectorized matrix-vector multiplication
        gufunc: tuple, Optional
            Tuple containing:
                1. String import path for a numpy/scipy function (e.g., "numpy.matmul", "scipy.special.softmax")
                that implements the blockwised operation of the scalar op.
                2 Number of inputs of the function
                3 Number of outputs of the function
        """
        if isinstance(core_op, Blockwise):
            raise TypeError("Core Op is already a Blockwise")

        if signature is None:
            signature = getattr(core_op, "gufunc_signature", None)
            if signature is None:
                raise ValueError(
                    f"Signature not provided nor found in core_op {core_op}"
                )

        self.core_op = core_op
        self.signature = signature
        self.name = name
        self.inputs_sig, self.outputs_sig = _parse_gufunc_signature(signature)
        self.gufunc_spec = gufunc_spec
        self._gufunc = None
        super().__init__(**kwargs)

    def __getstate__(self):
        d = copy(self.__dict__)
        d["_gufunc"] = None
        return d

    def _create_dummy_core_node(self, inputs: Sequence[TensorVariable]) -> Apply:
        core_input_types = []
        for i, (inp, sig) in enumerate(zip(inputs, self.inputs_sig)):
            if inp.type.ndim < len(sig):
                raise ValueError(
                    f"Input {i} {inp} has insufficient core dimensions for signature {self.signature}"
                )
            # ndim_supp = 0 case
            if not sig:
                core_shape = ()
            else:
                core_shape = inp.type.shape[-len(sig) :]
            core_input_types.append(tensor(dtype=inp.type.dtype, shape=core_shape))

        core_node = self.core_op.make_node(*core_input_types)

        if len(core_node.outputs) != len(self.outputs_sig):
            raise ValueError(
                f"Insufficient number of outputs for signature {self.signature}: {len(core_node.outputs)}"
            )
        for i, (core_out, sig) in enumerate(zip(core_node.outputs, self.outputs_sig)):
            if core_out.type.ndim != len(sig):
                raise ValueError(
                    f"Output {i} of {self.core_op} has wrong number of core dimensions for signature {self.signature}: {core_out.type.ndim}"
                )

        return core_node

    def make_node(self, *inputs):
        inputs = [as_tensor_variable(i) for i in inputs]

        core_node = self._create_dummy_core_node(inputs)

        batch_ndims = max(
            inp.type.ndim - len(sig) for inp, sig in zip(inputs, self.inputs_sig)
        )

        batched_inputs = []
        batch_shapes = []
        for i, (inp, sig) in enumerate(zip(inputs, self.inputs_sig)):
            # Append missing dims to the left
            missing_batch_ndims = batch_ndims - (inp.type.ndim - len(sig))
            if missing_batch_ndims:
                inp = shape_padleft(inp, missing_batch_ndims)
            batched_inputs.append(inp)

            if not sig:
                batch_shapes.append(inp.type.shape)
            else:
                batch_shapes.append(inp.type.shape[: -len(sig)])

        try:
            batch_shape = tuple(
                [
                    broadcast_static_dim_lengths(batch_dims)
                    for batch_dims in zip(*batch_shapes)
                ]
            )
        except ValueError:
            raise ValueError(
                f"Incompatible Blockwise batch input shapes {[inp.type.shape for inp in inputs]}"
            )

        batched_outputs = [
            tensor(dtype=core_out.type.dtype, shape=batch_shape + core_out.type.shape)
            for core_out in core_node.outputs
        ]

        return Apply(self, batched_inputs, batched_outputs)

    def batch_ndim(self, node: Apply) -> int:
        return cast(int, node.outputs[0].type.ndim - len(self.outputs_sig[0]))

    def infer_shape(
        self, fgraph, node, input_shapes
    ) -> list[tuple[TensorVariable, ...]]:
        from pytensor.tensor import broadcast_shape
        from pytensor.tensor.shape import Shape_i

        batch_ndims = self.batch_ndim(node)
        core_dims: dict[str, Any] = {}
        batch_shapes = [input_shape[:batch_ndims] for input_shape in input_shapes]
        for input_shape, sig in zip(input_shapes, self.inputs_sig):
            core_shape = input_shape[batch_ndims:]

            for core_dim, dim_name in zip(core_shape, sig):
                prev_core_dim = core_dims.get(core_dim)
                if prev_core_dim is None:
                    core_dims[dim_name] = core_dim
                # Prefer constants
                elif not isinstance(prev_core_dim, Constant):
                    core_dims[dim_name] = core_dim

        batch_shape = broadcast_shape(*batch_shapes, arrays_are_shapes=True)

        out_shapes = []
        for output, sig in zip(node.outputs, self.outputs_sig):
            core_out_shape = []
            for i, dim_name in enumerate(sig):
                # The output dim is the same as another input dim
                if dim_name in core_dims:
                    core_out_shape.append(core_dims[dim_name])
                else:
                    # TODO: We could try to make use of infer_shape of core_op
                    core_out_shape.append(Shape_i(batch_ndims + i)(output))
            out_shapes.append((*batch_shape, *core_out_shape))

        return out_shapes

    def connection_pattern(self, node):
        if hasattr(self.core_op, "connection_pattern"):
            return self.core_op.connection_pattern(node)

        return [[True for _ in node.outputs] for _ in node.inputs]

    def _bgrad(self, inputs, outputs, ograds):
        # Grad, with respect to broadcasted versions of inputs

        def as_core(t, core_t):
            # Inputs could be NullType or DisconnectedType
            if isinstance(t.type, NullType | DisconnectedType):
                return t
            return core_t.type()

        with config.change_flags(compute_test_value="off"):
            safe_inputs = [
                tensor(dtype=inp.type.dtype, shape=(None,) * len(sig))
                for inp, sig in zip(inputs, self.inputs_sig)
            ]
            core_node = self._create_dummy_core_node(safe_inputs)

            core_inputs = [
                as_core(inp, core_inp)
                for inp, core_inp in zip(inputs, core_node.inputs)
            ]
            core_ograds = [
                as_core(ograd, core_ograd)
                for ograd, core_ograd in zip(ograds, core_node.outputs)
            ]
            core_outputs = core_node.outputs

            core_igrads = self.core_op.L_op(core_inputs, core_outputs, core_ograds)

        igrads = vectorize_graph(
            [core_igrad for core_igrad in core_igrads if core_igrad is not None],
            replace=dict(
                zip(core_inputs + core_outputs + core_ograds, inputs + outputs + ograds)
            ),
        )

        igrads_iter = iter(igrads)
        return [
            None if core_igrad is None else next(igrads_iter)
            for core_igrad in core_igrads
        ]

    def L_op(self, inputs, outs, ograds):
        from pytensor.tensor.math import sum as pt_sum

        # Compute grad with respect to broadcasted input
        rval = self._bgrad(inputs, outs, ograds)

        # TODO: (Borrowed from Elemwise) make sure that zeros are clearly identifiable
        # to the gradient.grad method when the outputs have
        # some integer and some floating point outputs
        if any(out.type.dtype not in continuous_dtypes for out in outs):
            # For integer output, return value may only be zero or undefined
            # We don't bother with trying to check that the scalar ops
            # correctly returned something that evaluates to 0, we just make
            # the return value obviously zero so that gradient.grad can tell
            # this op did the right thing.
            new_rval = []
            for elem, inp in zip(rval, inputs):
                if isinstance(elem.type, NullType | DisconnectedType):
                    new_rval.append(elem)
                else:
                    elem = inp.zeros_like()
                    if str(elem.type.dtype) not in continuous_dtypes:
                        elem = elem.astype(config.floatX)
                    assert str(elem.type.dtype) not in discrete_dtypes
                    new_rval.append(elem)
            return new_rval

        # Sum out the broadcasted dimensions
        batch_ndims = self.batch_ndim(outs[0].owner)
        batch_shape = outs[0].type.shape[:batch_ndims]
        for i, (inp, sig) in enumerate(zip(inputs, self.inputs_sig)):
            if isinstance(rval[i].type, NullType | DisconnectedType):
                continue

            assert inp.type.ndim == batch_ndims + len(sig)

            to_sum = [
                j
                for j, (inp_s, out_s) in enumerate(zip(inp.type.shape, batch_shape))
                if inp_s == 1 and out_s != 1
            ]
            if to_sum:
                rval[i] = pt_sum(rval[i], axis=to_sum, keepdims=True)

        return rval

    def _create_gufunc(self, node):
        gufunc_spec = self.gufunc_spec or getattr(self.core_op, "gufunc_spec", None)

        if gufunc_spec is not None:
            self._gufunc = import_func_from_string(gufunc_spec[0])
            if self._gufunc:
                return self._gufunc
            else:
                raise ValueError(f"Could not import gufunc {gufunc_spec[0]} for {self}")

        n_outs = len(self.outputs_sig)
        core_node = self._create_dummy_core_node(node.inputs)

        def core_func(*inner_inputs):
            inner_outputs = [[None] for _ in range(n_outs)]

            inner_inputs = [np.asarray(inp) for inp in inner_inputs]
            self.core_op.perform(core_node, inner_inputs, inner_outputs)

            if len(inner_outputs) == 1:
                return inner_outputs[0][0]
            else:
                return tuple(r[0] for r in inner_outputs)

        self._gufunc = np.vectorize(core_func, signature=self.signature)
        return self._gufunc

    def _check_runtime_broadcast(self, node, inputs):
        batch_ndim = self.batch_ndim(node)

        for dims_and_bcast in zip(
            *[
                zip(input.shape[:batch_ndim], sinput.type.broadcastable[:batch_ndim])
                for input, sinput in zip(inputs, node.inputs)
            ]
        ):
            if any(d != 1 for d, _ in dims_and_bcast) and (1, False) in dims_and_bcast:
                raise ValueError(
                    "Runtime broadcasting not allowed. "
                    "At least one input has a distinct batch dimension length of 1, but was not marked as broadcastable.\n"
                    "If broadcasting was intended, use `specify_broadcastable` on the relevant input."
                )

    def perform(self, node, inputs, output_storage):
        gufunc = self._gufunc

        if gufunc is None:
            gufunc = self._create_gufunc(node)

        self._check_runtime_broadcast(node, inputs)

        res = gufunc(*inputs)
        if not isinstance(res, tuple):
            res = (res,)

        for node_out, out_storage, r in zip(node.outputs, output_storage, res):
            out_dtype = getattr(node_out, "dtype", None)
            if out_dtype and out_dtype != r.dtype:
                r = np.asarray(r, dtype=out_dtype)
            out_storage[0] = r

    def __str__(self):
        if self.name is None:
            return f"{type(self).__name__}{{{self.core_op}, {self.signature}}}"
        else:
            return self.name


@_vectorize_node.register(Op)
def vectorize_node_fallback(op: Op, node: Apply, *bached_inputs) -> Apply:
    for inp in node.inputs:
        if not isinstance(inp.type, TensorType | ScalarType):
            raise NotImplementedError(
                f"Cannot vectorize node {node} with input {inp} of type {inp.type}"
            )

    if hasattr(op, "gufunc_signature"):
        signature = op.gufunc_signature
    else:
        # TODO: This is pretty bad for shape inference and merge optimization!
        #  Should get better as we add signatures to our Ops
        signature = safe_signature(
            [inp.type.ndim for inp in node.inputs],
            [out.type.ndim for out in node.outputs],
        )
    return cast(Apply, Blockwise(op, signature=signature).make_node(*bached_inputs))


_vectorize_node.register(Blockwise, _vectorize_not_needed)


class OpWithCoreShape(OpFromGraph):
    """Generalizes an `Op` to include core shape as an additional input."""
