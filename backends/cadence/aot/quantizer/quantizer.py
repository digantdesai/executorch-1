# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

from typing import List, Optional, Tuple, Union

import torch
from executorch.backends.cadence.aot.quantizer.patterns import (
    AddmmPattern,
    BmmPattern,
    Conv1dPattern,
    Conv2dPattern,
    LayerNormPattern,
    LinearPattern,
    MatmulPattern,
    QuantizationPattern,
    ReluPattern0,
    ReluPattern1,
)
from executorch.backends.cadence.aot.quantizer.utils import (
    find_sequential_partitions_aten,
    is_annotated,
    no_outside_users,
)
from executorch.backends.xnnpack.quantizer.xnnpack_quantizer_utils import (
    OperatorConfig,
    QuantizationAnnotation,
    QuantizationConfig,
    QuantizationSpec,
)

from torch import fx

from torch.ao.quantization.observer import HistogramObserver, MinMaxObserver
from torch.ao.quantization.quantizer import DerivedQuantizationSpec, Quantizer
from torch.ao.quantization.quantizer.composable_quantizer import ComposableQuantizer


act_qspec = QuantizationSpec(
    dtype=torch.uint8,
    quant_min=0,
    quant_max=255,
    qscheme=torch.per_tensor_affine,
    is_dynamic=False,
    observer_or_fake_quant_ctr=HistogramObserver.with_args(eps=2**-12),
)

wgt_qspec = QuantizationSpec(
    dtype=torch.uint8,
    quant_min=0,
    quant_max=255,
    qscheme=torch.per_tensor_affine,
    is_dynamic=False,
    observer_or_fake_quant_ctr=MinMaxObserver,
)

bias_qspec: Optional[QuantizationSpec] = None

_default_qconfig = QuantizationConfig(
    act_qspec,
    act_qspec,
    wgt_qspec,
    None,
)


class CadenceAtenQuantizer(Quantizer):
    def __init__(
        self, pattern: QuantizationPattern, quantization_config: QuantizationConfig
    ) -> None:
        super().__init__()
        self.pattern = pattern
        self.quantization_config = quantization_config

    def annotate(self, model: torch.fx.GraphModule) -> torch.fx.GraphModule:
        fused_partitions = find_sequential_partitions_aten(
            model,
            self.pattern.partition_types(),
        )

        input_act_qspec = self.quantization_config.input_activation
        weight_qspec = self.quantization_config.weight
        bias_qspec = self.quantization_config.bias
        output_act_qspec = self.quantization_config.output_activation

        for fused_partition in fused_partitions:
            if not no_outside_users(fused_partition):
                continue

            anchors = self.pattern.get_anchors(model, fused_partition)
            if not anchors:
                continue
            if is_annotated(
                [
                    x[0]
                    for x in anchors.inputs
                    + anchors.weights
                    + anchors.biases
                    + anchors.output
                ]
            ):
                continue

            for output, *custom_spec in anchors.output:
                # pyre-ignore[16]: no attribute
                output.meta["quantization_annotation"] = QuantizationAnnotation(
                    # pyre-ignore[6]: incompatible parameter type
                    output_qspec=(custom_spec[0] if custom_spec else output_act_qspec),
                    _annotated=True,
                )

            def annotate_inputs(
                inputs: Union[
                    List[Tuple[fx.Node, int]],
                    List[Tuple[fx.Node, int, DerivedQuantizationSpec],],
                ],
                spec: Optional[QuantizationSpec],
            ) -> None:
                for node, idx, *custom_spec in inputs:
                    # pyre-ignore[16]: no attribute
                    annotation = node.meta.get(
                        "quantization_annotation",
                        QuantizationAnnotation(_annotated=True),
                    )
                    # pyre-ignore[16]: no attribute
                    annotation.input_qspec_map[node.args[idx]] = (
                        custom_spec[0] if custom_spec else spec
                    )
                    # pyre-ignore[16]: no attribute
                    node.meta["quantization_annotation"] = annotation

            annotate_inputs(anchors.inputs, input_act_qspec)
            annotate_inputs(anchors.weights, weight_qspec)
            # pyre-ignore[6]: incompatible parameter type
            annotate_inputs(anchors.biases, bias_qspec)
        return model

    def validate(self, model: fx.GraphModule) -> None:
        pass

    @classmethod
    def get_supported_operators(cls) -> List[OperatorConfig]:
        return []


def get_cadence_default_quantizer_list_with_config(
    quantization_config: QuantizationConfig,
) -> List[Quantizer]:
    return [
        CadenceAtenQuantizer(AddmmPattern(), quantization_config),
        CadenceAtenQuantizer(BmmPattern(), quantization_config),
        CadenceAtenQuantizer(Conv1dPattern(), quantization_config),
        CadenceAtenQuantizer(Conv2dPattern(), quantization_config),
        CadenceAtenQuantizer(LayerNormPattern(), quantization_config),
        CadenceAtenQuantizer(LinearPattern(), quantization_config),
        CadenceAtenQuantizer(MatmulPattern(), quantization_config),
        CadenceAtenQuantizer(ReluPattern0(), quantization_config),
        CadenceAtenQuantizer(ReluPattern1(), quantization_config),
    ]


class CadenceQuantizer(ComposableQuantizer):
    """
    Generic CadenceQuantizer. Although it can be used directly, it is typically a base
    class for explicitly defined quantizers (like CadenceDefaultQuantizer).
    """

    def __init__(self, quantizers: List[Quantizer]) -> None:
        super().__init__(quantizers)


class CadenceDefaultQuantizer(CadenceQuantizer):
    """
    Default quantizer for Cadence backend.
    """

    def __init__(self, qconfig: Optional[QuantizationConfig] = None) -> None:
        if qconfig is None:
            qconfig = _default_qconfig
        quantizers = get_cadence_default_quantizer_list_with_config(qconfig)
        super().__init__(quantizers)
