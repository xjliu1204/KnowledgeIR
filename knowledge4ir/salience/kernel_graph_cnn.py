"""
kernel pooling layer
init:
    v_mu: a 1-d dimension of mu's
    sigma: the sigma
input:
    similar to Linear()
        a n-D tensor, last dimension is the one to enforce kernel pooling
output:
    n-K tensor, K is the v_mu.size(), number of kernels
"""


import torch
import torch.nn as nn
from torch.autograd import Variable
from knowledge4ir.salience.utils import SalienceBaseModel
import logging
import json
import numpy as np
use_cuda = torch.cuda.is_available()


class KernelPooling(nn.Module):
    def __init__(self, l_mu=None, l_sigma=None):
        super(KernelPooling, self).__init__()
        if l_mu is None:
            l_mu = [1, 0.9, 0.7, 0.5, 0.3, 0.1, -0.1, -0.3, -0.5, -0.7, -0.9]
        self.v_mu = Variable(torch.FloatTensor(l_mu), requires_grad=False)
        self.K = len(l_mu)
        if l_sigma is None:
            l_sigma = [1e-3] + [0.1] * (self.v_mu.size()[-1] - 1)
        self.v_sigma = Variable(torch.FloatTensor(l_sigma), requires_grad=False)
        if use_cuda:
            self.v_mu = self.v_mu.cuda()
            self.v_sigma = self.v_sigma.cuda()
        logging.info('[%d] pooling kernels: %s',
                     self.K, json.dumps(zip(l_mu, l_sigma))
                     )
        return

    def forward(self, in_tensor, mtx_score):
        in_tensor = in_tensor.unsqueeze(-1)
        in_tensor = in_tensor.expand(in_tensor.size()[:-1] + (self.K,))
        score = -(in_tensor - self.v_mu) * (in_tensor - self.v_mu)
        kernel_value = torch.exp(score / (2.0 * self.v_sigma * self.v_sigma))
        mtx_score = mtx_score.unsqueeze(-1).unsqueeze(1)
        mtx_score = mtx_score.expand_as(kernel_value)
        weighted_kernel_value = kernel_value * mtx_score
        sum_kernel_value = torch.sum(weighted_kernel_value, dim=-2).clamp(min=1e-10)  # add entity freq/weight
        sum_kernel_value = torch.log(sum_kernel_value)
        return sum_kernel_value


class KernelGraphCNN(SalienceBaseModel):

    def __init__(self, para, pre_embedding=None):
        super(KernelGraphCNN, self).__init__(para, pre_embedding)
        l_mu, l_sigma = para.form_kernels()
        self.K = len(l_mu)
        self.kp = KernelPooling(l_mu, l_sigma)
        self.embedding = nn.Embedding(para.entity_vocab_size,
                                      para.embedding_dim, padding_idx=0)
        self.dropout = nn.Dropout(p=para.dropout_rate)
        self.linear = nn.Linear(self.K, 1, bias=True)
        if pre_embedding is not None:
            self.embedding.weight.data.copy_(torch.from_numpy(pre_embedding))
        if use_cuda:
            logging.info('copying parameter to cuda')
            self.embedding.cuda()
            self.kp.cuda()
            self.linear.cuda()
        self.layer = para.nb_hidden_layers
        return

    def forward(self, h_packed_data,):
        assert 'mtx_e' in h_packed_data
        assert 'mtx_score' in h_packed_data
        mtx_e = h_packed_data['mtx_e']
        mtx_score = h_packed_data['mtx_score']
        mtx_embedding = self.embedding(mtx_e)
        mtx_embedding = mtx_embedding.div(
            torch.norm(mtx_embedding, p=2, dim=-1, keepdim=True).expand_as(mtx_embedding) + 1e-8
        )

        trans_mtx = torch.matmul(mtx_embedding, mtx_embedding.transpose(-2, -1))
        trans_mtx = self.dropout(trans_mtx)
        kp_mtx = self.kp(trans_mtx, mtx_score)
        output = self.linear(kp_mtx)
        output = output.squeeze(-1)
        return output

    def save_model(self, output_name):
        logging.info('saving knrm embedding and linear weights to [%s]', output_name)
        emb_mtx = self.embedding.weight.data.cpu().numpy()
        np.save(open(output_name + '.emb.npy', 'w'), emb_mtx)
        np.save(open(output_name + '.linear.npy', 'w'),
                self.linear.weight.data.cpu().numpy())


class HighwayKCNN(KernelGraphCNN):
    def __init__(self, para, pre_embedding=None):
        super(HighwayKCNN, self).__init__(para, pre_embedding)
        self.linear_combine = nn.Linear(2, 1)
        if use_cuda:
            self.linear_combine.cuda()
        return

    def forward(self, h_packed_data,):
        assert 'mtx_e' in h_packed_data
        assert 'mtx_score' in h_packed_data
        mtx_e = h_packed_data['mtx_e']
        mtx_score = h_packed_data['mtx_score']
        knrm_res = super(HighwayKCNN, self).forward(mtx_e, mtx_score)
        mixed_knrm = torch.cat((knrm_res.unsqueeze(-1), mtx_score.unsqueeze(-1)), -1)
        output = self.linear_combine(mixed_knrm).squeeze(-1)
        return output


class KernelGraphWalk(SalienceBaseModel):
    """
    no working now... cannot converge. 9/19
    """
    def __init__(self, para, pre_embedding=None):
        super(KernelGraphWalk, self).__init__(para, pre_embedding)
        l_mu, l_sigma = para.form_kernels()
        self.K = len(l_mu)
        self.kp = KernelPooling(l_mu, l_sigma)
        self.embedding = nn.Embedding(para.entity_vocab_size,
                                      para.embedding_dim, padding_idx=0)
        self.layer = para.nb_hidden_layers
        self.l_linear = []
        for __ in xrange(self.layer):
            self.l_linear.append(nn.Linear(self.K, 1, bias=True))
        # self.linear = nn.Linear(self.K, 1, bias=True)
        if pre_embedding is not None:
            self.embedding.weight.data.copy_(torch.from_numpy(pre_embedding))
        if use_cuda:
            logging.info('copying parameter to cuda')
            self.embedding.cuda()
            self.kp.cuda()
            # self.linear.cuda()
            for linear in self.l_linear:
                linear.cuda()

        return

    def forward(self, h_packed_data,):
        assert 'mtx_e' in h_packed_data
        assert 'mtx_score' in h_packed_data
        mtx_e = h_packed_data['mtx_e']
        mtx_score = h_packed_data['mtx_score']
        mtx_embedding = self.embedding(mtx_e)
        mtx_embedding = mtx_embedding.div(
            torch.norm(mtx_embedding, p=2, dim=-1, keepdim=True).expand_as(mtx_embedding) + 1e-8
        )

        trans_mtx = torch.matmul(mtx_embedding, mtx_embedding.transpose(-2, -1))
        output = mtx_score
        for linear in self.l_linear:
            kp_mtx = self.kp(trans_mtx, output)
            output = linear(kp_mtx)
            output = output.squeeze(-1)
        if use_cuda:
            return output.cuda()
        else:
            return output
