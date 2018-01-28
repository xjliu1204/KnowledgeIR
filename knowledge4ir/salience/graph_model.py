import torch
import torch.nn as nn
from torch.autograd import Variable
from knowledge4ir.salience.base import SalienceBaseModel, KernelPooling
from knowledge4ir.salience.knrm_vote import KNRM
from knowledge4ir.salience.crf_model import (
    LinearKernelCRF,
)
from knowledge4ir.salience.masked_knrm_vote import (
    MaskKNRM,
)
import logging
import json
import torch.nn.functional as F
import numpy as np

use_cuda = torch.cuda.is_available()


class MaskKernelCrf(LinearKernelCRF):
    def __init__(self, para, ext_data=None):
        super(MaskKernelCrf, self).__init__(para, ext_data)
        self.use_mask = para.use_mask
        if self.use_mask:
            logging.info('Running model with masking on empty slots.')
        else:
            logging.info('Running model without masking.')

    def forward(self, h_packed_data):
        mtx_e = h_packed_data['mtx_e']
        ts_feature = h_packed_data['ts_feature']

        if ts_feature.size()[-1] != self.node_feature_dim:
            logging.error('feature shape: %s != feature dim [%d]',
                          json.dumps(ts_feature.size()), self.node_feature_dim)
        assert ts_feature.size()[-1] == self.node_feature_dim
        if mtx_e.size()[:2] != ts_feature.size()[:2]:
            logging.error(
                'e mtx and feature tensor shape do not match: %s != %s',
                json.dumps(mtx_e.size()), json.dumps(ts_feature.size()))
        assert mtx_e.size()[:2] == ts_feature.size()[:2]

        node_score = F.tanh(self.node_lr(ts_feature))

        # frequency is the first dim of feature, always
        # mtx_score = ts_feature.narrow(-1, 0, 1).squeeze(-1)
        mtx_score = h_packed_data['mtx_score']

        h_mid_data = {
            "mtx_e": mtx_e,
            "mtx_score": mtx_score
        }

        if self.use_mask:
            mtx_e_mask = h_packed_data['masks']['mtx_e']
            mtx_embedding = self.embedding(mtx_e)
            mtx_masked_embedding = mtx_embedding * mtx_e_mask.unsqueeze(-1)
            kp_mtx = self._kernel_scores(mtx_masked_embedding, mtx_score)
            knrm_res = self.linear(kp_mtx).squeeze(-1)
        else:
            knrm_res = super(LinearKernelCRF, self).forward(h_mid_data)

        mixed_knrm = torch.cat((knrm_res.unsqueeze(-1), node_score), -1)
        output = self.linear_combine(mixed_knrm).squeeze(-1)
        return output


class StructEventKernelCRF(MaskKNRM):
    def __init__(self, para, ext_data=None):
        super(StructEventKernelCRF, self).__init__(para, ext_data)
        self.embedding_dim = para.embedding_dim
        self.node_feature_dim = para.node_feature_dim
        self.node_lr = nn.Linear(self.node_feature_dim, 1, bias=False)
        logging.info('node feature dim %d', self.node_feature_dim)

        self.use_mask = para.use_mask

        if self.use_mask:
            logging.info('Running model with masking on empty slots.')
        else:
            logging.info('Running model without masking.')

        if use_cuda:
            self.node_lr.cuda()

    def forward(self, h_packed_data):
        ts_feature = h_packed_data['ts_feature']

        if ts_feature.size()[-1] != self.node_feature_dim:
            logging.error('feature shape: %s != feature dim [%d]',
                          json.dumps(ts_feature.size()), self.node_feature_dim)
        assert ts_feature.size()[-1] == self.node_feature_dim

        output = self.compute_score(h_packed_data)
        return output

    def event_embedding(self, mtx_evm, ts_args, mtx_arg_length, ts_arg_mask):
        return self.embedding(mtx_evm)

    def compute_score(self, h_packed_data):
        mtx_e, mtx_e_mask, mtx_score, node_score = self.get_features(
            h_packed_data)

        if self.use_mask:
            knrm_res = self._forward_kernel_with_mask_and_features(
                mtx_e_mask, mtx_e, mtx_score, node_score)
        else:
            knrm_res = self._forward_kernel_with_features(mtx_e, mtx_score,
                                                          node_score)
        return knrm_res

    def get_features(self, h_packed_data):
        ts_feature = h_packed_data['ts_feature']
        mtx_e = h_packed_data['mtx_e']
        mtx_evm = h_packed_data['mtx_evm']

        masks = h_packed_data['masks']
        mtx_e_mask = masks['mtx_e']
        mtx_evm_mask = masks['mtx_evm']

        mtx_e_embedding = self.embedding(mtx_e)
        if mtx_evm is None:
            # For documents without events.
            combined_mtx_e = mtx_e_embedding
            combined_mtx_e_mask = mtx_e_mask
        else:
            ts_args = h_packed_data['ts_args']
            mtx_arg_length = h_packed_data['mtx_arg_length']
            ts_arg_mask = masks['ts_args']
            mtx_evm_embedding = self.event_embedding(mtx_evm, ts_args,
                                                     mtx_arg_length,
                                                     ts_arg_mask)

            combined_mtx_e = torch.cat((mtx_e_embedding, mtx_evm_embedding), 1)
            combined_mtx_e_mask = torch.cat((mtx_e_mask, mtx_evm_mask), 1)

        node_score = F.tanh(self.node_lr(ts_feature))
        mtx_score = h_packed_data['mtx_score']

        return combined_mtx_e, combined_mtx_e_mask, mtx_score, node_score

    def _argument_sum(self, ts_args, ts_arg_mask):
        l_evm_embedding = []

        for mtx_args, mask in zip(ts_args, ts_arg_mask):
            mtx_args_embedding = self.embedding(mtx_args)
            masked_embedding = mtx_args_embedding * mask.unsqueeze(-1)
            arg_embedding_sum = masked_embedding.sum(1)
            l_evm_embedding.append(arg_embedding_sum)

        return torch.stack(l_evm_embedding)

    def save_model(self, output_name):
        logging.info('saving knrm embedding and linear weights to [%s]',
                     output_name)
        super(StructEventKernelCRF, self).save_model(output_name)


class AverageEventKernelCRF(StructEventKernelCRF):
    def __init__(self, para, ext_data=None):
        super(AverageEventKernelCRF, self).__init__(para, ext_data)

    def event_embedding(self, mtx_evm, ts_args, mtx_arg_length, ts_arg_mask):
        mtx_p_embedding = self.embedding(mtx_evm)

        if ts_args is None:
            # When there are no arguments, the embedding is just the predicate.
            mtx_evm_embedding_aver = mtx_p_embedding
        else:
            mtx_arg_embedding_sum = self._argument_sum(ts_args, ts_arg_mask)
            mtx_evm_embedding_sum = mtx_p_embedding + mtx_arg_embedding_sum

            # aver = (embedding sum) / (1 + arg length)
            mtx_full_length = (mtx_arg_length + 1).type_as(
                mtx_evm_embedding_sum).unsqueeze(2)
            mtx_evm_embedding_aver = mtx_evm_embedding_sum / mtx_full_length
        return mtx_evm_embedding_aver


class AverageArgumentKernelCRF(StructEventKernelCRF):
    def __init__(self, para, ext_data=None):
        super(AverageArgumentKernelCRF, self).__init__(para, ext_data)
        self.args_linear = nn.Linear(self.embedding_dim, self.embedding_dim)
        self.evm_arg_linear = nn.Linear(self.embedding_dim * 2,
                                        self.embedding_dim)

        if use_cuda:
            self.args_linear.cuda()
            self.evm_arg_linear.cuda()

    def event_embedding(self, mtx_evm, ts_args, mtx_arg_length, ts_arg_mask):
        mtx_p_embedding = self.embedding(mtx_evm)

        if ts_args is None:
            mtx_arg = torch.zeros(mtx_p_embedding.size())
            if use_cuda:
                mtx_arg = mtx_arg.cuda()
        else:
            mtx_arg_embedding_sum = self._argument_sum(ts_args, ts_arg_mask)

            # Remove zero lengths.
            mtx_arg_length[mtx_arg_length == 0] = 1

            broadcast_length = mtx_arg_length.unsqueeze(2).type_as(
                mtx_arg_embedding_sum)
            # Average argument embedding.
            mtx_arg_embedding_aver = mtx_arg_embedding_sum / broadcast_length

            # Non linearly map the argument embeddings.
            mtx_arg = F.tanh(self.args_linear(mtx_arg_embedding_aver))

        mtx_evm_args_cat = torch.cat((mtx_p_embedding, mtx_arg), 2)

        # Non linearly combine event and argument embeddings.
        return F.tanh(self.evm_arg_linear(mtx_evm_args_cat))


class GraphCNNKernelCRF(StructEventKernelCRF):
    def __init__(self, para, ext_data=None):
        super(GraphCNNKernelCRF, self).__init__(para, ext_data)

    def compute_score(self, h_packed_data):
        laplacian = h_packed_data['ts_laplacian']
        mtx_e, mtx_e_mask, mtx_score, node_score = self.get_features(
            h_packed_data)

        if self.use_mask:
            kp_mtx = self._masked_kernel_scores(mtx_e_mask,
                                                mtx_e, mtx_score)
        else:
            kp_mtx = self._kernel_scores(mtx_e, mtx_score)

        features = torch.cat((kp_mtx, node_score), -1)
        gcnn_features = torch.bmm(laplacian, features)

        if self.debug:

            gcnn_score = self.linear(gcnn_features).squeeze(-1)

            print "GCNN score"
            print gcnn_score

            origin_score = self.linear(features).squeeze(-1)
            print "Original score"
            print origin_score

            diff_score = gcnn_score - origin_score
            print 'Diff score'
            print diff_score

            import sys
            sys.stdin.readline()

        output = self.linear(gcnn_features).squeeze(-1)

        return output

    def _feature_size(self):
        return self.K + 1


class ConcatGraphCNNKernelCRF(StructEventKernelCRF):
    def __init__(self, para, ext_data=None):
        super(ConcatGraphCNNKernelCRF, self).__init__(para, ext_data)

    def compute_score(self, h_packed_data):
        laplacian = h_packed_data['ts_laplacian']
        mtx_e, mtx_e_mask, mtx_score, node_score = self.get_features(
            h_packed_data)

        if self.use_mask:
            kp_mtx = self._masked_kernel_scores(mtx_e_mask,
                                                mtx_e, mtx_score)
        else:
            kp_mtx = self._kernel_scores(mtx_e, mtx_score)

        features = torch.cat((kp_mtx, node_score), -1)
        gcnn_features = torch.bmm(laplacian, features)
        full_features = torch.cat((features, gcnn_features), -1)

        output = self.linear(full_features).squeeze(-1)

        return output

    def _feature_size(self):
        return (self.K + 1) * 2
