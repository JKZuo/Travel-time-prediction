# -- coding: utf-8 --
import tensorflow as tf
import pandas as pd
import numpy as np
import os
import argparse
import datetime

from model.embedding import embedding
from model.trajectory_inference import DeepFM
from model.data_next import DataClass
from model.utils import construct_feed_dict, one_hot_concatenation, metric, FC, STEmbedding
from model.st_block import ST_Block
from model.bridge import BridgeTransformer
from model.inference import InferenceClass

tf.reset_default_graph()
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
logs_path = "board"


class Model(object):
    def __init__(self, hp):
        self.hp = hp
        self.step = self.hp.step  # window length
        self.epoch = self.hp.epoch  # total training epochs
        self.dropout = self.hp.dropout  # dropout
        self.site_num = self.hp.site_num  # number of roads
        self.emb_size = self.hp.emb_size  # hidden embedding size
        self.is_training = self.hp.is_training
        self.field_cnt = self.hp.field_cnt  # number of features fields
        self.feature_s = self.hp.feature_s  # number of speed features
        self.batch_size = self.hp.batch_size  # batch size
        self.feature_tra = self.hp.feature_tra  # number of trajectory features
        self.divide_ratio = self.hp.divide_ratio  # the ratio of training set
        self.input_length = self.hp.input_length  # input length of speed data
        self.output_length = self.hp.output_length  # output length of speed data
        self.learning_rate = self.hp.learning_rate  # learning rate
        self.trajectory_length = self.hp.trajectory_length  # trajectory length

        self.initial_placeholder()
        self.initial_speed_embedding()
        self.model()

    def initial_placeholder(self):
        # define placeholders
        self.placeholders = {
            'position': tf.placeholder(tf.int32, shape=(1, self.site_num), name='input_position'),
            'week': tf.placeholder(tf.int32, shape=(None, self.site_num), name='input_week'),
            'day': tf.placeholder(tf.int32, shape=(None, self.site_num), name='input_day'),
            'hour': tf.placeholder(tf.int32, shape=(None, self.site_num), name='input_hour'),
            'minute': tf.placeholder(tf.int32, shape=(None, self.site_num), name='input_minute'),
            'feature_s': tf.placeholder(tf.float32, shape=[None, self.input_length, self.site_num, self.feature_s], name='input_s'),
            'label_s': tf.placeholder(tf.float32, shape=[None, self.site_num, self.output_length], name='label_s'),
            'feature_tra': tf.placeholder(tf.float32, shape=[None, self.feature_tra], name='input_tra'),
            'label_tra': tf.placeholder(tf.float32, shape=[None, self.trajectory_length], name='label_tra'),
            'label_tra_sum': tf.placeholder(tf.float32, shape=[None, 1], name='label_tra_sum'),
            'feature_inds': tf.placeholder(dtype=tf.int32, shape=[None, self.field_cnt], name='feature_inds'),
            'trajectory_inds': tf.placeholder(dtype=tf.int32, shape=[self.trajectory_length], name='feature_inds'),
            'dropout': tf.placeholder_with_default(0., shape=(), name='input_dropout')
        }

    def initial_speed_embedding(self):
        # speed related embedding define
        p_emd = embedding(self.placeholders['position'], vocab_size=self.site_num, num_units=self.emb_size, scale=False,
                          scope="position_embed")
        self.p_emd = tf.tile(tf.expand_dims(p_emd, axis=0),
                             [self.batch_size, self.input_length + self.output_length, 1, 1])

        w_emb = embedding(self.placeholders['week'], vocab_size=5, num_units=self.emb_size, scale=False,
                          scope="week_embed")
        self.w_emd = tf.reshape(w_emb, shape=[self.batch_size, self.input_length + self.output_length, self.site_num,
                                              self.emb_size])

        d_emb = embedding(self.placeholders['day'], vocab_size=31, num_units=self.emb_size, scale=False,
                          scope="day_embed")
        self.d_emd = tf.reshape(d_emb, shape=[self.batch_size, self.input_length + self.output_length, self.site_num,
                                              self.emb_size])

        h_emb = embedding(self.placeholders['hour'], vocab_size=24, num_units=self.emb_size, scale=False,
                          scope="hour_embed")
        self.h_emd = tf.reshape(h_emb, shape=[self.batch_size, self.input_length + self.output_length, self.site_num,
                                              self.emb_size])

        m_emb = embedding(self.placeholders['minute'], vocab_size=4, num_units=self.emb_size, scale=False,
                          scope="minute_embed")
        self.m_emd = tf.reshape(m_emb, shape=[self.batch_size, self.input_length + self.output_length, self.site_num,
                                              self.emb_size])

    def model(self):
        '''
        :param batch_size: 64
        :param encoder_layer:
        :param decoder_layer:
        :param encoder_nodes:
        :param prediction_size:
        :param is_training: True
        :return:
        '''
        with tf.variable_scope(name_or_scope='speed_model'):
            timestamp = [self.h_emd, self.m_emd]
            position = self.p_emd
            speed = FC(self.placeholders['feature_s'], units=[self.emb_size, self.emb_size],
                       activations=[tf.nn.relu, None],
                       bn=False, bn_decay=0.99, is_training=self.is_training)
            STE = STEmbedding(position, timestamp, 0, self.emb_size, False, 0.99, self.is_training)
            st_block = ST_Block(hp=self.hp, placeholders=self.placeholders)
            encoder_outs = st_block.spatio_temporal(speed=speed, STE=STE[:, :self.input_length, :, :])
            print('ST_Block outs shape is : ', encoder_outs.shape)  # (32, 12, 108, 64)

            bridge = BridgeTransformer(self.hp)
            bridge_outs = bridge.encoder(X=encoder_outs,
                                         X_P=encoder_outs,
                                         X_Q=STE[:, self.input_length:, :, :])
            print('BridgeTransformer outs shape is : ', bridge_outs.shape)  # (32, 12, 108, 64)
            encoder_outs = tf.concat([encoder_outs, bridge_outs], axis=1)
            hidden_states = tf.gather(encoder_outs, indices=self.placeholders['trajectory_inds'],
                                      axis=2)  # (32, 24, 5, 64)
            print(hidden_states.shape)
            inference = InferenceClass(para=self.hp)
            self.pre_s = inference.inference(out_hiddens=bridge_outs)
            print('Inference outs shape is : ', self.pre_s.shape)  # (32, 108, 12)

        print('#................................feature cross....................................#')
        with tf.variable_scope(name_or_scope='trajectory_model'):
            DeepModel = DeepFM(self.hp)
            self.pre_tra_sep, self.pre_tra_sum = DeepModel.inference(X=self.placeholders['feature_tra'],
                                                                       feature_inds=self.placeholders['feature_inds'],
                                                                       keep_prob=self.placeholders['dropout'],
                                                                       hiddens=hidden_states)
        self.loss1 = tf.reduce_mean(
            tf.sqrt(tf.reduce_mean(tf.square(self.pre_s + 1e-10 - self.placeholders['label_s']), axis=0)))
        self.loss2 = tf.reduce_mean(
            tf.sqrt(tf.reduce_mean(tf.square(self.pre_tra_sum + 1e-10 - self.placeholders['label_tra_sum']), axis=0)))
        self.loss3 = tf.reduce_mean(
            tf.sqrt(tf.reduce_mean(tf.square(self.pre_tra_sep + 1e-10 - self.placeholders['label_tra']), axis=0)))

        self.loss = 0.3 * self.loss1 + 0.4 * self.loss2 + 0.3 * self.loss3
        self.train_op = tf.train.AdamOptimizer(self.learning_rate).minimize(self.loss)

        print('#...............................in the training step.....................................#')

    def test(self):
        '''
        :param batch_size: usually use 1
        :param encoder_layer:
        :param decoder_layer:
        :param encoder_nodes:
        :param prediction_size:
        :param is_training: False
        :return:
        '''
        model_file = tf.train.latest_checkpoint('weights/')
        self.saver.restore(self.sess, model_file)

    def initialize_session(self):
        self.sess = tf.Session()
        self.saver = tf.train.Saver(var_list=tf.trainable_variables())

    def re_current(self, a, max, min):
        return a * (max - min) + min

    def run_epoch(self):
        '''
        from now on,the model begin to training, until the epoch to 100
        '''
        start_time = datetime.datetime.now()
        max_mae = 100
        self.sess.run(tf.global_variables_initializer())
        iterate = DataClass(self.hp)
        train_next = iterate.next_batch(batch_size=self.batch_size, epoch=self.epoch, is_training=True)

        for i in range(int(iterate.shape_tra[0] * self.divide_ratio) * self.epoch // self.batch_size):
            x_s, week, day, hour, minute, label_s, \
            vehicle_id, vehicle_type, start_week, start_day, start_hour, start_minute, start_second, distances, route_id, \
            element_index, separate_trajectory_time, total_time, trajectory_inds,_ = self.sess.run(train_next)

            x_s = np.reshape(x_s, [-1, self.input_length, self.site_num, self.feature_s])
            week = np.reshape(week, [-1, self.site_num])
            day = np.reshape(day, [-1, self.site_num])
            hour = np.reshape(hour, [-1, self.site_num])
            minute = np.reshape(minute, [-1, self.site_num])
            x_tra = one_hot_concatenation(
                features=[vehicle_id, vehicle_type, start_week, start_day, start_hour, start_minute, start_second,
                          distances, route_id])
            feed_dict = construct_feed_dict(x_s=x_s,
                                            week=week,
                                            day=day,
                                            hour=hour,
                                            minute=minute,
                                            label_s=label_s,
                                            x_tra=x_tra,
                                            element_index=element_index,
                                            separate_trajectory_time=separate_trajectory_time,
                                            total_time=total_time,
                                            trajectory_inds=trajectory_inds,
                                            placeholders=self.placeholders)
            feed_dict.update({self.placeholders['dropout']: self.dropout})

            loss, _ = self.sess.run((self.loss, self.train_op), feed_dict=feed_dict)
            # loss1, _ = self.sess.run((self.loss1, self.train_op), feed_dict=feed_dict)
            # loss, _ = self.sess.run((self.loss2, self.train_op), feed_dict=feed_dict)
            print("after %d steps,the training average loss value is : %.6f" % (i, loss))

            # validate processing
            if i % 100 == 0:
                mae = self.evaluate()
                if max_mae > mae:
                    print("the validate average loss value is : %.6f" % (mae))
                    max_mae = mae
                    self.saver.save(self.sess, save_path=self.hp.save_path + 'model.ckpt')
        end_time = datetime.datetime.now()
        total_time = end_time - start_time
        print("Total running times is : %f" % total_time.total_seconds())

    def evaluate(self):
        '''
        :param para:
        :param pre_model:
        :return:
        '''
        label_s_list, pre_s_list = list(), list()
        label_tra_sum_list, pre_tra_sum_list = list(), list()
        label_tra_sep_list, pre_tra_sep_list = list(), list()

        # with tf.Session() as sess:
        model_file = tf.train.latest_checkpoint(self.hp.save_path)
        if not self.hp.is_training:
            print('the model weights has been loaded:')
            self.saver.restore(self.sess, model_file)

        iterate_test = DataClass(hp=self.hp)
        test_next = iterate_test.next_batch(batch_size=self.batch_size, epoch=1, is_training=False)
        max_s, min_s = iterate_test.max_s['speed'], iterate_test.min_s['speed']

        for i in range(int(iterate_test.shape_tra[0] * (1 - self.hp.divide_ratio) - 15 * (
                self.input_length + self.output_length)) // self.batch_size):
            x_s, week, day, hour, minute, label_s, \
            vehicle_id, vehicle_type, start_week, start_day, start_hour, start_minute, start_second, distances, route_id, \
            element_index, separate_trajectory_time, total_time, trajectory_inds, dates = self.sess.run(test_next)
            x_s = np.reshape(x_s, [-1, self.input_length, self.site_num, self.feature_s])
            week = np.reshape(week, [-1, self.site_num])
            day = np.reshape(day, [-1, self.site_num])
            hour = np.reshape(hour, [-1, self.site_num])
            minute = np.reshape(minute, [-1, self.site_num])
            x_tra = one_hot_concatenation(
                features=[vehicle_id, vehicle_type, start_week, start_day, start_hour, start_minute, start_second,
                          distances, route_id])
            feed_dict = construct_feed_dict(x_s=x_s,
                                            week=week,
                                            day=day,
                                            hour=hour, minute=minute,
                                            label_s=label_s,
                                            x_tra=x_tra,
                                            element_index=element_index,
                                            separate_trajectory_time=separate_trajectory_time,
                                            total_time=total_time,
                                            trajectory_inds=trajectory_inds,
                                            placeholders=self.placeholders)
            feed_dict.update({self.placeholders['dropout']: 0.0})
            pre_s, pre_tra_sep, pre_tra_sum = self.sess.run((self.pre_s, self.pre_tra_sep, self.pre_tra_sum), feed_dict=feed_dict)
            # print(dates, pre_tra_sum * 60, total_time * 60)
            label_tra_sum_list.append(total_time)
            pre_tra_sum_list.append(pre_tra_sum)
            label_tra_sep_list.append(separate_trajectory_time)
            pre_tra_sep_list.append(pre_tra_sep)
            label_s_list.append(label_s)
            pre_s_list.append(pre_s)

        label_tra_sum_list = np.reshape(np.array(label_tra_sum_list, dtype=np.float32) * 60, [-1, 1])  # total trajectory travel time for label
        pre_tra_sum_list = np.reshape(np.array(pre_tra_sum_list, dtype=np.float32) * 60, [-1, 1])  # total trajectory travel time for prediction

        label_tra_sep_list = np.reshape(np.array(label_tra_sep_list, dtype=np.float32) * 60, [-1, 1])  # seperate trajectory travel time for label
        pre_tra_sep_list = np.reshape(np.array(pre_tra_sep_list, dtype=np.float32) * 60, [-1, 1])  # seperate trajectory travel time for prediction

        label_s_list = np.reshape(np.array(label_s_list, dtype=np.float32), [-1, self.site_num, self.output_length]).transpose([1, 0, 2])
        pre_s_list = np.reshape(np.array(pre_s_list, dtype=np.float32), [-1, self.site_num, self.output_length]).transpose([1, 0, 2])
        if self.hp.normalize:
            label_s_list = self.re_current(label_s_list, max_s, min_s)
            pre_s_list = self.re_current(pre_s_list, max_s, min_s)

        print('speed prediction result >>>')
        mae_s, rmse_s, mape_s, cor_s, r2_s = metric(pre_s_list, label_s_list)  # 产生预测指标

        print('entire travel time prediction result >>>')
        mae_tra_sum, rmse_tra_sum, mape_tra_sum, cor_tra_sum, r2_tra_sum = metric(pred=pre_tra_sum_list, label=label_tra_sum_list)  # 产生预测指标
        # describe(label_list, predict_list)   #预测值可视化

        print('seperate travel time prediction result >>>')
        mae_tra_sep, rmse_tra_sep, mape_tra_sep, cor_tra_sep, r2_tra_sep = metric(pred=pre_tra_sep_list, label=label_tra_sep_list)  # 产生预测指标

        return mae_tra_sum