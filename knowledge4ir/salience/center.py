"""
model I/O, train, and testing center
train:
    hashed nyt data
        three field:
            docno:
            body: l_e
            abstract: l_e
    and train the model
test:
    hashed nyt data
    output the scores for entities in body

hyper-parameters:
    mini-batch size
    learning rate
    vocabulary size
    embedding dim

"""

import json
import logging
import math
import os

import numpy as np
import torch
from traitlets import (
    Unicode,
    Int,
    Float,
    List,
    Bool
)
from traitlets.config import Configurable

from knowledge4ir.salience.base import NNPara, ExtData
from knowledge4ir.salience.baseline.node_feature import (
    FrequencySalience,
    FeatureLR,
)
from knowledge4ir.salience.baseline.translation_model import (
    EmbPageRank,
    EdgeCNN,
)
from knowledge4ir.salience.crf_model import (
    KernelCRF,
    LinearKernelCRF,
)
from knowledge4ir.salience.graph_model import (
    AverageEventKernelCRF,
    AverageArgumentKernelCRF,
)
from knowledge4ir.salience.utils.data_io import DataIO
from knowledge4ir.salience.deprecated.adj_knrm import AdjKNRM
from knowledge4ir.salience.deprecated.duet import DuetGlossCNN
from knowledge4ir.salience.deprecated.local_context import (
    LocalAvgWordVotes,
    LocalRNNVotes,
    LocalRNNMaxSim,
)
from knowledge4ir.salience.deprecated.node_feature import EmbeddingLR
from knowledge4ir.salience.duet_knrm import DuetKNRM, GlossCNNEmbDuet
from knowledge4ir.salience.external_semantics.description import GlossCNNKNRM
from knowledge4ir.salience.external_semantics.nlss import NlssCnnKnrm
from knowledge4ir.salience.knrm_vote import KNRM
from knowledge4ir.salience.utils.data_io import (
    raw_io,
    feature_io,
    uw_io,
    duet_io,
    adj_edge_io,
)
from knowledge4ir.salience.utils.evaluation import SalienceEva
from knowledge4ir.salience.utils.ranking_loss import (
    hinge_loss,
    pairwise_loss,
)
from knowledge4ir.utils import (
    body_field,
    add_svm_feature,
    mutiply_svm_feature,
    salience_gold
)

use_cuda = torch.cuda.is_available()


class SalienceModelCenter(Configurable):
    learning_rate = Float(1e-3, help='learning rate').tag(config=True)
    model_name = Unicode(help="model name: trans").tag(config=True)
    nb_epochs = Int(2, help='nb of epochs').tag(config=True)
    l_class_weights = List(Float, default_value=[1, 10]).tag(config=True)
    batch_size = Int(128, help='number of documents per batch').tag(config=True)
    loss_func = Unicode('hinge',
                        help='loss function to use: hinge, pairwise').tag(
        config=True)
    early_stopping_patient = Int(5, help='epochs before early stopping').tag(
        config=True)
    early_stopping_frequency = Int(100000000,
                                   help='the nb of data points to check dev loss'
                                   ).tag(config=True)
    max_e_per_doc = Int(200, help='max e per doc')

    # The following 3 configs should be deprecated with the old io.
    # event_model = Bool(False, help='Run event model').tag(config=True)
    joint_model = Bool(False, help='Run joint model').tag(config=True)
    # input_format = Unicode(help='overwrite input format: raw | featured').tag(
    #     config=True)
    # The above 3 configs should be deprecated with the old io.

    use_new_io = Bool(True, help='whether use the new IO format').tag(
        config=True
    )
    predict_with_intermediate_res = Bool(
        False, help='whether to kee intermediate results').tag(config=True)

    h_model = {
        'frequency': FrequencySalience,
        'feature_lr': FeatureLR,
        "trans": EmbPageRank,
        'knrm': KNRM,
        'linear_kcrf': LinearKernelCRF,

        'gloss_cnn': GlossCNNKNRM,
        'nlss_cnn': NlssCnnKnrm,
        'duet_knrm': DuetKNRM,
        'duet_gloss': DuetGlossCNN,
        'gloss_enriched_duet': GlossCNNEmbDuet,
        'adj_knrm': AdjKNRM,

        'kcrf_event_average': AverageEventKernelCRF,
        'kcrf_args_average': AverageArgumentKernelCRF,

        "avg_local_vote": LocalAvgWordVotes,  # not working
        'local_rnn': LocalRNNVotes,  # not working
        'local_max_rnn': LocalRNNMaxSim,  # not working
        'EdgeCNN': EdgeCNN,  # not working
        'lr': EmbeddingLR,  # not working
        'kcrf': KernelCRF,  # not working
    }

    h_model_io = {
        'frequency': raw_io,
        'feature_lr': feature_io,
        'knrm': raw_io,
        'linear_kcrf': feature_io,
        'gloss_cnn': raw_io,
        'nlss_cnn': raw_io,
        'word_knrm': duet_io,
        'duet_knrm': duet_io,
        'duet_gloss': duet_io,
        'gloss_enriched_duet': duet_io,
        'adj_knrm': adj_edge_io,

        "avg_local_vote": uw_io,  # not working
        'local_rnn': uw_io,  # not working
        'local_max_rnn': uw_io,  # not working
        'lr': feature_io,  # not working
    }

    # in_field = Unicode(body_field)
    spot_field = Unicode('spot')
    event_spot_field = Unicode('event')
    abstract_field = Unicode('abstract')
    # A specific field is reserved to mark the salience answer.
    salience_gold = Unicode(salience_gold)

    def __init__(self, **kwargs):
        super(SalienceModelCenter, self).__init__(**kwargs)
        self.para = NNPara(**kwargs)
        self.ext_data = ExtData(**kwargs)
        self.ext_data.assert_with_para(self.para)
        self._setup_io(**kwargs)
        h_loss = {
            "hinge": hinge_loss,  # hinge classification loss does not work
            "pairwise": pairwise_loss,
        }
        self.criterion = h_loss[self.loss_func]
        self.class_weight = torch.cuda.FloatTensor(self.l_class_weights)

        # if self.event_model and self.joint_model:
        #     logging.error("Please specify one mode only.")
        #     exit(1)

        self.evaluator = SalienceEva(**kwargs)
        self._init_model()

        self.patient_cnt = 0
        self.best_valid_loss = 0
        self.ll_valid_line = []

    def _setup_io(self, **kwargs):
        self.io_parser = DataIO(**kwargs)

    @classmethod
    def class_print_help(cls, inst=None):
        super(SalienceModelCenter, cls).class_print_help(inst)
        NNPara.class_print_help(inst)
        ExtData.class_print_help(inst)
        SalienceEva.class_print_help(inst)
        DataIO.class_print_help(inst)

    def _init_model(self):
        if self.model_name:
            if self.joint_model:
                self._merge_para()
            self.model = self.h_model[self.model_name](self.para, self.ext_data)
            logging.info('use model [%s]', self.model_name)

    def _merge_para(self):
        """
        Merge the parameter of entity and event embedding, including the vocab
        size.
        :return:
        """
        self.ext_data.entity_emb = np.concatenate((self.ext_data.entity_emb,
                                                   self.ext_data.event_emb))
        self.para.entity_vocab_size = self.para.entity_vocab_size + \
                                      self.para.event_vocab_size

        assert self.para.node_feature_dim == self.io_parser.e_feature_dim + \
               self.io_parser.evm_feature_dim

        logging.info("Embedding matrix merged into shape [%d,%d]" % (
            self.ext_data.entity_emb.shape[0],
            self.ext_data.entity_emb.shape[1]))

    def train(self, train_in_name, validation_in_name=None,
              model_out_name=None):
        """
        train using the given data
        will use each doc as the mini-batch for now
        :param train_in_name: training data
        :param validation_in_name: validation data
        :param model_out_name: name to dump the model
        :return: keep the model
        """
        logging.info('training with data in [%s]', train_in_name)
        self.model.train()

        if not model_out_name:
            model_out_name = train_in_name + '.model_%s' % self.model_name

        logging.info('Model out name is [%s]', model_out_name)

        model_dir = os.path.dirname(model_out_name)
        if not os.path.exists(model_dir):
            os.makedirs(model_dir)

        if validation_in_name:
            self._init_early_stopper(validation_in_name)

        optimizer = torch.optim.Adam(
            filter(lambda model_para: model_para.requires_grad,
                   self.model.parameters()),
            lr=self.learning_rate
        )
        l_epoch_loss = []
        for epoch in xrange(self.nb_epochs):
            self._epoch_start()

            p = 0
            total_loss = 0
            data_cnt = 0
            logging.info('start epoch [%d]', epoch)
            l_this_batch_line = []
            es_cnt = 0
            es_flag = False
            for line in open(train_in_name):
                if self.io_parser.is_empty_line(line):
                    continue
                data_cnt += 1
                es_cnt += 1
                l_this_batch_line.append(line)
                if len(l_this_batch_line) >= self.batch_size:
                    this_loss = self._batch_train(l_this_batch_line,
                                                  self.criterion, optimizer)
                    p += 1
                    total_loss += this_loss
                    logging.debug('[%d] batch [%f] loss', p, this_loss)
                    assert not math.isnan(this_loss)
                    if not p % 100:
                        logging.info('batch [%d] [%d] data, average loss [%f]',
                                     p, data_cnt, total_loss / p)
                        self._train_info()
                    l_this_batch_line = []
                    if es_cnt >= self.early_stopping_frequency:
                        logging.info(
                            'checking dev loss at [%d]-[%d] vs frequency [%d]',
                            epoch, es_cnt,
                            self.early_stopping_frequency)
                        es_cnt = 0
                        if validation_in_name:
                            self.model.eval()
                            if self._early_stop(model_out_name):
                                logging.info(
                                    'early stopped at [%d] epoch [%d] data',
                                    epoch, data_cnt)
                                es_flag = True
                                break
                            self.model.train()
            if es_flag:
                break

            if l_this_batch_line:
                this_loss = self._batch_train(l_this_batch_line, self.criterion,
                                              optimizer)
                p += 1
                total_loss += this_loss
                logging.debug('[%d] batch [%f] loss', p, this_loss)
                assert not math.isnan(this_loss)
                l_this_batch_line = []

            logging.info(
                'epoch [%d] finished with loss [%f] on [%d] batch [%d] doc',
                epoch, total_loss / p, p, data_cnt)
            l_epoch_loss.append(total_loss / p)

            self._train_info()

            # validation
            if validation_in_name:
                self.model.eval()
                if self._early_stop(model_out_name):
                    logging.info('early stopped at [%d] epoch', epoch)
                    break
                self.model.train()

        logging.info('[%d] epoch done with loss %s', self.nb_epochs,
                     json.dumps(l_epoch_loss))

        if model_out_name:
            # self.model.save_model(model_out_name)
            logging.info('Torch saving model to [%s]', model_out_name)
            torch.save(self.model, model_out_name)
        return

    def _train_info(self):
        pass

    def _epoch_start(self):
        pass

    def _epoch_end(self):
        pass

    def _init_early_stopper(self, validation_in_name):
        self.patient_cnt = 0
        self.best_valid_loss = None
        self.ll_valid_line = []
        logging.info('loading validation data from [%s]', validation_in_name)
        l_valid_lines = [l for l in open(validation_in_name).read().splitlines()
                         if not self.io_parser.is_empty_line(l)]
        self.ll_valid_line = [l_valid_lines[i:i + self.batch_size]
                              for i in
                              xrange(0, len(l_valid_lines), self.batch_size)]
        logging.info('validation with [%d] doc', len(l_valid_lines))
        self.best_valid_loss = sum([self._batch_test(l_one_batch)
                                    for l_one_batch in self.ll_valid_line]
                                   ) / float(len(self.ll_valid_line))
        logging.info('initial validation loss [%.4f]', self.best_valid_loss)

    def _early_stop(self, model_out_name):
        this_valid_loss = sum([self._batch_test(l_one_batch)
                               for l_one_batch in self.ll_valid_line]
                              ) / float(len(self.ll_valid_line))
        logging.info('valid loss [%f]', this_valid_loss)
        if self.best_valid_loss is None:
            self.best_valid_loss = this_valid_loss
            logging.info('init valid loss with [%f]', this_valid_loss)
            if model_out_name:
                logging.info('save init model to [%s]', model_out_name)
                torch.save(self.model, model_out_name)
                logging.info('model kept')
        elif this_valid_loss > self.best_valid_loss:
            self.patient_cnt += 1
            logging.info('valid loss increased [%.4f -> %.4f][%d]',
                         self.best_valid_loss, this_valid_loss,
                         self.patient_cnt)
            if self.patient_cnt >= self.early_stopping_patient:
                logging.info('early stopped after patient [%d]',
                             self.patient_cnt)
                logging.info('loading best model [%s] with loss [%f]',
                             model_out_name, self.best_valid_loss)
                self.model = torch.load(model_out_name)
                return True
        else:
            self.patient_cnt = 0
            logging.info('valid loss decreased [%.4f -> %.4f][%d]',
                         self.best_valid_loss, this_valid_loss,
                         self.patient_cnt)
            if model_out_name:
                logging.info('update best model at [%s]', model_out_name)
                torch.save(self.model, model_out_name)
                logging.info('model kept')

            self.best_valid_loss = this_valid_loss
        return False

    def load_model(self, model_out_name):
        logging.info('loading trained model from [%s]', model_out_name)
        self.model = torch.load(model_out_name)

    def _batch_train(self, l_line, criterion, optimizer):
        h_packed_data, m_label = self._data_io(l_line)
        optimizer.zero_grad()
        output = self.model(h_packed_data)
        loss = criterion(output, m_label)
        loss.backward()
        optimizer.step()
        assert not math.isnan(loss.data[0])
        return loss.data[0]

    def predict(self, test_in_name, label_out_name, debug=False):
        """
        predict the data in test_in,
        dump predict labels in label_out_name
        :param test_in_name:
        :param label_out_name:
        :param debug:
        :return:
        """
        res_dir = os.path.dirname(label_out_name)
        if not os.path.exists(res_dir):
            os.makedirs(res_dir)

        self.model.debug_mode(debug)
        self.model.eval()

        out = open(label_out_name, 'w')
        logging.info('start predicting for [%s]', test_in_name)
        p = 0
        h_total_eva = dict()
        for line in open(test_in_name):
            if self.io_parser.is_empty_line(line):
                continue
            h_out, h_this_eva = self._per_doc_predict(line)
            if h_out is None:
                continue
            h_total_eva = add_svm_feature(h_total_eva, h_this_eva)
            print >> out, json.dumps(h_out)
            p += 1
            h_mean_eva = mutiply_svm_feature(h_total_eva, 1.0 / p)
            if not p % 1000:
                logging.info('predicted [%d] docs, eva %s', p,
                             json.dumps(h_mean_eva))
        h_mean_eva = mutiply_svm_feature(h_total_eva, 1.0 / max(p, 1.0))
        l_mean_eva = h_mean_eva.items()
        l_mean_eva.sort(key=lambda item: item[0])
        logging.info('finished predicted [%d] docs, eva %s', p,
                     json.dumps(l_mean_eva))
        json.dump(
            l_mean_eva,
            open(label_out_name + '.eval', 'w'),
            indent=1
        )
        out.close()
        return

    def _per_doc_predict(self, line):
        h_info = json.loads(line)
        key_name = 'docno'
        if key_name not in h_info:
            key_name = 'qid'
            assert key_name in h_info
        docno = h_info[key_name]
        h_packed_data, v_label = self._data_io([line])
        v_e = h_packed_data['mtx_e']
        # v_w = h_packed_data['mtx_score']
        if (not v_e[0].size()) | (not v_label[0].size()):
            return None, None
        output = self.model(h_packed_data).cpu()[0]
        v_e = v_e[0].cpu()

        pre_label = output.data.sign().type(torch.LongTensor)
        l_score = output.data.numpy().tolist()
        h_out = dict()
        h_out[key_name] = docno
        l_e = v_e.data.numpy().tolist()
        h_out[self.io_parser.content_field] = {'predict': zip(l_e, l_score)}

        if self.predict_with_intermediate_res:
            middle_output = \
                self.model.forward_intermediate(h_packed_data).cpu()[0]
            l_middle_features = middle_output.data.numpy().tolist()
            h_out[self.io_parser.content_field][
                'predict_features'] = zip(l_e, l_middle_features)

        v_label = v_label[0].cpu()
        y = v_label.data.view_as(pre_label)
        l_label = y.numpy().tolist()
        h_this_eva = self.evaluator.evaluate(l_score, l_label)
        h_out['eval'] = h_this_eva
        return h_out, h_this_eva

    def _batch_test(self, l_lines):
        h_packed_data, m_label = self._data_io(l_lines)
        output = self.model(h_packed_data)
        loss = self.criterion(output, m_label)
        return loss.data[0]

    def _data_io(self, l_line):
        if self.use_new_io:
            return self.model.data_io(l_line, self.io_parser)
        else:
            return self._old_io(l_line)

    def _old_io(self, l_line):
        return self.h_model_io[self.model_name](
            l_line,
            self.para.node_feature_dim,
            self.spot_field,
            self.in_field,
            self.abstract_field,
            self.salience_gold,
            self.max_e_per_doc
        )


if __name__ == '__main__':
    import sys
    from knowledge4ir.utils import (
        set_basic_log,
        load_py_config,
    )


    # set_basic_log(logging.INFO)

    class Main(Configurable):
        train_in = Unicode(help='training data').tag(config=True)
        test_in = Unicode(help='testing data').tag(config=True)
        test_out = Unicode(help='test res').tag(config=True)
        valid_in = Unicode(help='validation in').tag(config=True)
        model_out = Unicode(help='model dump out name').tag(config=True)
        log_level = Unicode('INFO', help='log level').tag(config=True)
        debug = Bool(False, help='Debug mode').tag(config=True)


    if 2 != len(sys.argv):
        print "unit test model train test"
        print "1 para, config"
        SalienceModelCenter.class_print_help()
        Main.class_print_help()
        sys.exit(-1)

    conf = load_py_config(sys.argv[1])
    para = Main(config=conf)

    set_basic_log(logging.getLevelName(para.log_level))

    model = SalienceModelCenter(config=conf)
    model.train(para.train_in, para.valid_in, para.model_out)
    model.predict(para.test_in, para.test_out, para.debug)
