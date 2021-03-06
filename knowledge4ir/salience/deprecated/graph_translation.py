import json
import logging

import numpy as np
import torch
from torch import nn as nn
from torch.nn import functional as F
use_cuda = torch.cuda.is_available()


class GraphTranslation(nn.Module):
    """
    input: list of document entities + frequency, target e id
    output: p(target e id is salient)
    deprecated and replaced by translation_model.EmbPageRank
    """

    def __init__(self, layer, vocab_size, embedding_dim, pre_embedding=None):
        super(GraphTranslation, self).__init__()
        self.embedding = nn.Embedding(vocab_size, embedding_dim)
        self.linear = nn.Linear(1, 2, bias=True)
        if use_cuda:
            logging.info('copying parameter to cuda')
            self.embedding.cuda()
            self.linear.cuda()
        self.layer = layer
        if pre_embedding is not None:
            self.embedding.weight.data.copy_(torch.from_numpy(pre_embedding))
        return

    def forward(self, v_e, v_score):
        """
        return probability of each one being salient
        :param v_e: the input entity id's, has to be Variable()
        :param v_score: the initial weights on each entity, has to be Variable()
        :return: score for each one
        """
        mtx_embedding = self.embedding(v_e)
        # mtx_embedding += 0.001  # protect zero norm
        mtx_embedding = mtx_embedding.div(
            torch.norm(mtx_embedding, p=2, dim=1).unsqueeze(-1).expand_as(mtx_embedding))

        trans_mtx = torch.mm(mtx_embedding, mtx_embedding.transpose(0, 1)) + 1.0
        trans_mtx = trans_mtx.div(
            torch.norm(trans_mtx, p=1, dim=0).unsqueeze(0).expand_as(trans_mtx)
        )
        mid = trans_mtx.cpu().data.numpy()
        if np.sum(np.isnan(mid)):
            logging.info('entities:\n%s',
                         json.dumps(v_e.data.cpu().numpy().tolist())
                         )
            logging.info('init scores:\n%s',
                         json.dumps(v_score.data.cpu().numpy().tolist())
                         )
            logging.info('linear weights:\n%s',
                         json.dumps(self.linear.weight.data.cpu().numpy().tolist()))
            logging.info('embeddings are:\n %s',
                         json.dumps(mtx_embedding.data.cpu().numpy().tolist())
                         )
            logging.info('trans_mtx:\n %s',
                         json.dumps(trans_mtx.data.cpu().numpy().tolist())
                         )

            raise ValueError
        output = v_score.unsqueeze(-1)
        for p in xrange(self.layer):
            output = torch.mm(trans_mtx, output)

        output = F.log_softmax(self.linear(output))
        output = output.squeeze(-1)
        if use_cuda:
            return output.cuda()
        else:
            return output