import torch
import torch.nn as nn
import torch.utils.checkpoint as checkpoint
from typing import List

from graphnet.models.components.layers import FourierEncoder, SpacetimeEncoder, Block_rel, Block
from graphnet.models.gnn.dynedge import DynEdge
from graphnet.models.gnn.gnn import GNN

from timm.models.layers import trunc_normal_

from torch_geometric.nn.pool import knn_graph
from torch_geometric.utils import to_dense_batch
from torch_geometric.data import Data
from torch import Tensor

def convert_data(data: Data):
    """Convert the input data to a tensor of shape (B, L, D)"""
    x_list = torch.split(data.x, data.n_pulses.tolist())
    x = torch.nn.utils.rnn.pad_sequence(x_list, batch_first=True, padding_value=torch.inf)
    mask = torch.ne(x[:,:,1], torch.inf)
    x[~mask] = 0
    return x, mask

class DeepIce(GNN):
    def __init__(
        self,
        dim: int = 384,
        dim_base: int = 128,
        depth: int = 12,
        head_size: int = 32,
        depth_rel: int = 4,
        n_rel: int = 1,
        max_pulses: int = 768,
    ):
        super().__init__(dim_base, dim)
        self.fourier_ext = FourierEncoder(dim_base, dim)
        self.rel_pos = SpacetimeEncoder(head_size)
        self.sandwich = nn.ModuleList(
            [Block_rel(dim=dim, num_heads=dim // head_size) for i in range(depth_rel)]
        )
        self.cls_token = nn.Linear(dim, 1, bias=False)
        self.blocks = nn.ModuleList(
            [
                Block(
                    dim=dim,
                    num_heads=dim // head_size,
                    mlp_ratio=4,
                    drop_path=0.0 * (i / (depth - 1)),
                    init_values=1,
                )
                for i in range(depth)
            ]
        )
        self.n_rel = n_rel
        
    @torch.jit.ignore
    def no_weight_decay(self):
        return {"cls_token"}

    def forward(self, data: Data) -> Tensor:
        x0, mask = convert_data(data)
        n_pulses = data.n_pulses
        x = self.fourier_ext(x0, n_pulses)
        rel_pos_bias, rel_enc = self.rel_pos(x0)
        batch_size = mask.shape[0]
        attn_mask = torch.zeros(mask.shape, device=mask.device)
        attn_mask[~mask] = -torch.inf

        for i, blk in enumerate(self.sandwich):
            x = blk(x, attn_mask, rel_pos_bias)
            if i + 1 == self.n_rel:
                rel_pos_bias = None

        mask = torch.cat(
            [torch.ones(batch_size, 1, dtype=mask.dtype, device=mask.device), mask], 1
        )
        attn_mask = torch.zeros(mask.shape, device=mask.device)
        attn_mask[~mask] = -torch.inf
        cls_token = self.cls_token.weight.unsqueeze(0).expand(batch_size, -1, -1)
        x = torch.cat([cls_token, x], 1)

        for blk in self.blocks:
            x = blk(x, None, attn_mask)

        return x[:, 0]
    
    
class DeepIceWithDynEdge(GNN):
    def __init__(
        self,
        dim: int = 384,
        dim_base: int = 128,
        depth: int = 8,
        head_size: int = 64,
        features_subset: List[int] = [0, 1, 2],
        max_pulses: int = 768,
    ):
        super().__init__(dim_base, dim)
        self.features_subset = features_subset
        self.fourier_ext = FourierEncoder(dim_base, dim // 2, scaled=True)
        self.rel_pos = SpacetimeEncoder(head_size)
        self.sandwich = nn.ModuleList(
            [
                Block_rel(dim=dim, num_heads=dim // head_size),
                Block_rel(dim=dim, num_heads=dim // head_size),
                Block_rel(dim=dim, num_heads=dim // head_size),
                Block_rel(dim=dim, num_heads=dim // head_size),
            ]
        )
        self.cls_token = nn.Linear(dim, 1, bias=False)
        self.blocks = nn.ModuleList(
            [
                Block(
                    dim=dim,
                    num_heads=dim // head_size,
                    mlp_ratio=4,
                    drop_path=0.0 * (i / (depth - 1)),
                    init_values=1,
                )
                for i in range(depth)
            ]
        )
        self.dyn_edge = DynEdge(
            9,
            post_processing_layer_sizes=[336, dim // 2],
            dynedge_layer_sizes=[(128, 256), (336, 256), (336, 256), (336, 256)],
            global_pooling_schemes=None
        )
        
    @torch.jit.ignore
    def no_weight_decay(self):
        return {"cls_token"}

    def forward(self, data: Data) -> Tensor:
        x0, mask = convert_data(data)
        graph_feature = torch.concat(
            [
                data.pos[mask],
                data.time[mask].view(-1, 1),
                data.auxiliary[mask].view(-1, 1),
                data.qe[mask].view(-1, 1),
                data.charge[mask].view(-1, 1),
                data.ice_properties[mask],
            ],
            dim=1,
        )
        Lmax = mask.sum(-1).max()
        x = self.fourier_ext(data, Lmax)
        rel_pos_bias, rel_enc = self.rel_pos(data, Lmax)
        mask = mask[:, :Lmax]
        batch_index = mask.nonzero()[:, 0]
        edge_index = knn_graph(x=graph_feature[:, self.features_subset], k=8, batch=batch_index).to(
            mask.device
        )
        graph_feature = self.dyn_edge(
            graph_feature, edge_index, batch_index, data.n_pulses
        )
        graph_feature, _ = to_dense_batch(graph_feature, batch_index)

        B, _ = mask.shape
        attn_mask = torch.zeros(mask.shape, device=mask.device)
        attn_mask[~mask] = -torch.inf
        x = torch.cat([x, graph_feature], 2)

        for blk in self.sandwich:
            x = blk(x, attn_mask, rel_pos_bias)
            if self.knn_features == 3:
                rel_pos_bias = None
        mask = torch.cat(
            [torch.ones(B, 1, dtype=mask.dtype, device=mask.device), mask], 1
        )
        attn_mask = torch.zeros(mask.shape, device=mask.device)
        attn_mask[~mask] = -torch.inf
        cls_token = self.cls_token.weight.unsqueeze(0).expand(B, -1, -1)
        x = torch.cat([cls_token, x], 1)

        for blk in self.blocks:
            x = blk(x, None, attn_mask)

        return x[:, 0]