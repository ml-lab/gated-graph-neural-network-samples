#!/usr/bin/env/python
'''
Usage:
    chem_tensorflow_sparse.py [options]

Options:
    -h --help                Show this screen.
    --config-file FILE       Hyperparameter configuration file path (in JSON format)
    --config CONFIG          Hyperparameter configuration dictionary (in JSON format)
    --out NAME               Log out file name
    --out_dir NAME           Lot out dir name
    --data_dir NAME          data dir name
'''
from typing import List

from docopt import docopt
from collections import defaultdict
import numpy as np
import tensorflow as tf
import time
import os
import json
import queue
import threading

import sys, traceback
import pdb


########################################################################################
# Download data
########################################################################################

def load_data(file_name, data_dir, task_id, restrict=-1, tie_fwd_bkwd=True):
    full_path = os.path.join(data_dir, file_name)

    print("loading data from: ", full_path)
    with open(full_path, 'r') as f:
        data = json.load(f)

    if restrict > 0:
        data = data[:restrict]

    x_dim = len(data[0]["node_features"][0])

    processed_graphs = []
    for d in data:
        processed_graphs.append({
            'adjacency_lists': graph_to_adjacency_lists(d['graph'], tie_fwd_bkwd),
            'init': d["node_features"],
            'label': d["targets"][task_id][0]
        })

    return processed_graphs, x_dim

########################################################################################
# Preprocess data
########################################################################################

def graph_string_to_array(graph_string):
    graph = []
    for s in graph_string.split('\n'):
        graph.append([int(v) for v in s.split(' ')])
    return graph

def graph_to_adjacency_lists(graph, tie_fwd_bkwd=True):
    adj_lists = defaultdict(list)
    for src, e, dest in graph:
        adj_lists[e - 1].append((src, dest))  # Make edges start from 0
        if tie_fwd_bkwd:
            adj_lists[e - 1].append((dest, src))  # Make edges start from 0

    final_adj_lists = {e: np.array(sorted(lm), dtype=np.int32)
                       for e, lm in adj_lists.items()}

    # Add backward edges as an additional edge type that goes backwards:
    if not(tie_fwd_bkwd):
        num_edge_types = len(adj_lists)
        for (edge_type, edges) in adj_lists.items():
            bwd_edge_type = num_edge_types + edge_type
            final_adj_lists[bwd_edge_type] = np.array(sorted((y, x) for (x, y) in edges), dtype=np.int32)

    return final_adj_lists


########################################################################################
# GNN model
########################################################################################

class SparseGGNN:
    def __init__(self, params):
        self.params = params
        self.num_edge_types = self.params['n_edge_types']
        h_dim = self.params['hidden_size']
        self.edge_weights = tf.Variable(self.init_weights([self.num_edge_types * h_dim, h_dim]),
                                        name='graph_edge_weights')

        if self.params['use_edge_bias']:
            self.edge_biases = tf.Variable(np.zeros([self.num_edge_types, h_dim], dtype=np.float32),
                                           name='graph_edge_biases')

        with tf.variable_scope("graph_gru"):
            self.gru_node = tf.nn.rnn_cell.GRUCell(h_dim)

    @staticmethod
    def init_weights(shape):
        return np.sqrt(6.0 / (shape[-2] + shape[-1])) * (2 * np.random.rand(*shape).astype(np.float32) - 1)

    def sparse_gnn_layer(self,
                         node_embeddings: tf.Tensor,
                         adjacency_lists: List[tf.Tensor],
                         num_incoming_edges: tf.Tensor) -> tf.Tensor:
        """
        Run through a GNN and return the representations of the nodes.
        :param node_embeddings: the initial embeddings of the nodes.
        :param adjacency_lists: a list of *sorted* adjacency indexes per edge type
        :param num_incoming_edges: [v, num_edge_types] tensor indicating number of incoming edges per type
        :return: the representations of the nodes
        """
        h_dim = self.params['hidden_size']  # embedding_size (D)

        with tf.variable_scope('gnn_scope'):
            cur_node_states = node_embeddings  # number of nodes in batch v x D
            num_nodes = tf.shape(node_embeddings, out_type=tf.int64)[0]

            adjacency_matrices = []  # type: List[tf.SparseTensor]
            for adjacency_list_for_edge_type in adjacency_lists:
                # adjacency_list_for_edge_type (shape [-1, 2]) includes all edges of type e_type of a sparse graph with v nodes (ids from 0 to v).
                adjacency_matrix_for_edge_type = tf.SparseTensor(indices=adjacency_list_for_edge_type,
                                                                 values=tf.ones_like(adjacency_list_for_edge_type[:, 1],
                                                                                     dtype=tf.float32),
                                                                 dense_shape=[num_nodes, num_nodes])
                adjacency_matrices.append(adjacency_matrix_for_edge_type)

            for _ in range(self.params['unrolling_steps']):
                incoming_messages = []  # list of v x D

                # Collect incoming messages per edge type
                for adjacency_matrix in adjacency_matrices:
                    incoming_messages_per_type = tf.sparse_tensor_dense_matmul(adjacency_matrix, cur_node_states)  # v x D
                    incoming_messages.extend([incoming_messages_per_type])

                # Pass incoming messages through linear layer:
                incoming_messages = tf.concat(incoming_messages, axis=1)  # v x [2 *] edge_types
                messages_passed = tf.matmul(incoming_messages, self.edge_weights)  # v x D
                if self.params['use_edge_bias']:
                    messages_passed += tf.matmul(num_incoming_edges, self.edge_biases)  # v x D

                # pass updated vertex features into GRU
                cur_node_states = self.gru_node(messages_passed, cur_node_states)[0]  # v x D

            return cur_node_states

########################################################################################
# Chem Wrapper of GNN model
########################################################################################
class ChemGNN:
    def __init__(self, params):
        self.__node_features = tf.placeholder(tf.float32, [None, params['hidden_size']], name='node_features')
        self.__target_values = tf.placeholder(tf.float32, [None], name='targets')
        self.__num_graphs = tf.placeholder(tf.int64, [], name='num_graphs')
        self.__dropout_keep_prob = tf.placeholder(tf.float32, [], name='dropout_keep_prob')

        self.__gnn = SparseGGNN(params)
        num_edge_types = params['n_edge_types']
        self.__adjacency_lists = [tf.placeholder(tf.int64, [None, 2], name='adjacency_e%s' % e)
                                  for e in range(num_edge_types)]
        self.__num_incoming_edges = tf.placeholder(tf.float32, [None, num_edge_types], name='num_incoming_edges')

        self.__graph_nodes_list = tf.placeholder(tf.int64, [None, 2], name='graph_nodes_list')

        self.__regression_gate = MLP(2 * params['hidden_size'], 1, [], self.__dropout_keep_prob)
        self.__regression_transform = MLP(params['hidden_size'], 1, [], self.__dropout_keep_prob)

        self.__loss, self.__accuracy = self.get_loss()

        self.__optimizer = tf.train.AdamOptimizer()

        grads_and_vars = self.__optimizer.compute_gradients(self.__loss)
        clipped_grads = []
        for grad, var in grads_and_vars:
            if grad is not None:
                clipped_grads.append((tf.clip_by_norm(grad, params['clamp_gradient_norm']), var))
            else:
                clipped_grads.append((grad, var))
        self.train_op = self.__optimizer.apply_gradients(clipped_grads)

    @property
    def node_features(self):
        return self.__node_features

    @property
    def dropout_keep_prob(self):
        return self.__dropout_keep_prob

    @property
    def num_graphs(self):
        return self.__num_graphs

    @property
    def target_values(self):
        return self.__target_values

    @property
    def adjacency_lists(self):
        return self.__adjacency_lists

    @property
    def num_incoming_edges(self):
        return self.__num_incoming_edges

    @property
    def graph_nodes_list(self):
        return self.__graph_nodes_list

    @property
    def loss(self):
        return self.__loss

    @property
    def accuracy(self):
        return self.__accuracy

    def gated_regression(self, last_h):
        # last_h: [v x h]
        gate_input = tf.concat([last_h, self.__node_features], axis=-1)  # [v x 2h]

        gated_outputs = tf.nn.sigmoid(self.__regression_gate(gate_input)) * self.__regression_transform(last_h)  # [v x 1]

        # Sum up all nodes per-graph
        num_nodes = tf.shape(gate_input, out_type=tf.int64)[0]
        graph_nodes = tf.SparseTensor(indices=self.__graph_nodes_list,
                                      values=tf.ones_like(self.__graph_nodes_list[:, 0], dtype=tf.float32),
                                      dense_shape=[self.__num_graphs, num_nodes])  # [g x v]
        return tf.squeeze(tf.sparse_tensor_dense_matmul(graph_nodes, gated_outputs), axis=[-1])  # [g]

    def get_loss(self):
        node_representations = self.__gnn.sparse_gnn_layer(self.__node_features,
                                                           self.__adjacency_lists,
                                                           self.__num_incoming_edges)
        computed_representations = self.gated_regression(node_representations)
        diff = computed_representations - self.__target_values
        loss = tf.reduce_mean(0.5 * (diff) ** 2)
        accuracy = tf.reduce_mean(tf.abs(diff))
        return loss, accuracy


class MLP(object):
    def __init__(self, in_size, out_size, hid_sizes, dropout_keep_prob):
        self.in_size = in_size
        self.out_size = out_size
        self.hid_sizes = hid_sizes
        self.dropout_keep_prob = dropout_keep_prob
        self.params = self.make_network_params()

    def make_network_params(self):
        dims = [self.in_size] + self.hid_sizes + [self.out_size]
        weight_sizes = list(zip(dims[:-1], dims[1:]))
        weights = [tf.Variable(self.init_weights(s)) for s in weight_sizes]
        biases = [tf.Variable(np.zeros(s[-1]).astype(np.float32)) for s in weight_sizes]

        network_params = {
            "weights": weights,
            "biases": biases,
        }

        return network_params

    def init_weights(self, shape):
        return np.sqrt(6.0 / (shape[-2] + shape[-1])) * (2 * np.random.rand(*shape).astype(np.float32) - 1)

    def __call__(self, inputs):
        acts = inputs
        for W, b in zip(self.params["weights"], self.params["biases"]):
            hid = tf.matmul(acts, tf.nn.dropout(W, self.dropout_keep_prob)) + b
            acts = tf.nn.relu(hid)
        last_hidden = hid
        return last_hidden

########################################################################################
# Training
########################################################################################
def default_params():
    return {
        'batch_size': 100000,
        'clamp_gradient_norm': 1.0,
        'optimizer': 'adam', # adam or fixed
        'hidden_size': 100,
        'use_edge_bias': True,
        'unrolling_steps': 4,
        'out': 'log.json',
        'task_id': 0,
        'restrict_data': -1,
        'do_validation': True,
        'dropout_keep_prob': 1.0,
        'tie_fwd_bkwd': True,
    }

def make_params(args):
    params = default_params()
    config_file = args.get('--config-file')
    if config_file is not None:
        with open(config_file, 'r') as f:
            params.update(json.load(f))
    config = args.get('--config')
    if config is not None:
        params.update(json.loads(config))
    print(params)
    return params

def get_data(args, params):
    data_dir = ''
    if '--data_dir' in args and args['--data_dir'] is not None:
        data_dir = args['--data_dir']

    data = {}
    data['train'], x_dim = load_data("molecules_train.json", data_dir, params['task_id'], restrict=params["restrict_data"], tie_fwd_bkwd=params['tie_fwd_bkwd'])
    data['valid'], x_dim = load_data("molecules_valid.json", data_dir, params['task_id'], restrict=params["restrict_data"], tie_fwd_bkwd=params['tie_fwd_bkwd'])

    n_edge_types = max(len(d['adjacency_lists']) for d in data['train'])

    params.update({
        'n_edge_types': n_edge_types,
        'annotation_size': x_dim,
        'data_dir': data_dir
        })
    return data

def update_output_path(args, params):
    out_dir = args.get('--out_dir') or ''
    out_file = args.get('--out')
    if out_file is not None:
        params['out'] = os.path.join(out_dir, out_file)


class ThreadedIterator:
    """An iterator object that computes its elements in a parallel thread to be ready to be consumed.
    The iterator should *not* return None"""

    def __init__(self, original_iterator, max_queue_size: int=2):
        self.__queue = queue.Queue(maxsize=max_queue_size)
        self.__thread = threading.Thread(target=lambda: self.worker(original_iterator))
        self.__thread.start()

    def worker(self, original_iterator):
        for element in original_iterator:
            assert element is not None, 'By convention, iterator elements much not be None'
            self.__queue.put(element, block=True)
        self.__queue.put(None, block=True)

    def __iter__(self):
        next_element = self.__queue.get(block=True)
        while next_element is not None:
            yield next_element
            next_element = self.__queue.get(block=True)
        self.__thread.join()


def training_loop(sess, model: ChemGNN, data, params, is_training):
    chemical_accuracy = [0.066513725,0.012235489,0.071939046,0.033730778,0.033486113,0.004278493,0.001330901,0.004165489,0.004128926,0.00409976,0.004527465,0.012292586,0.037467458]

    def minibatch_iterator():
        """Create minibatches by flattening adjacency matrices into a single adjacency matrix with
        multiple disconnected components."""
        if is_training:
            np.random.shuffle(data)
        # Pack until we cannot fit more graphs in the batch
        num_graphs = 0
        while num_graphs < len(data):
            num_graphs_in_batch = 0
            batch_node_features = []
            batch_graph_target_values = []
            batch_adjacency_lists = [[] for _ in range(params['n_edge_types'])]
            batch_num_incoming_edges_dicts = [defaultdict(lambda: 0) for _ in range(params['n_edge_types'])]
            batch_graph_nodes_list = []
            node_offset = 0

            while num_graphs < len(data) and node_offset + len(data[num_graphs]['init']) < params['batch_size']:
                num_nodes_in_graph = len(data[num_graphs]['init'])
                padded_features = np.pad(data[num_graphs]['init'],
                                         ((0, 0), (0, params['hidden_size'] - params['annotation_size'])),
                                         'constant')
                batch_node_features.extend(padded_features)
                batch_graph_nodes_list.extend((num_graphs_in_batch, node_offset + i) for i in range(num_nodes_in_graph))
                for i in range(params['n_edge_types']):
                    if i in data[num_graphs]['adjacency_lists']:
                        batch_adjacency_lists[i].append(data[num_graphs]['adjacency_lists'][i] + node_offset)

                        for edge_idx in range(data[num_graphs]['adjacency_lists'][i].shape[0]):
                            batch_num_incoming_edges_dicts[i][data[num_graphs]['adjacency_lists'][i][edge_idx,1] + node_offset] += 1
                batch_graph_target_values.append(data[num_graphs]['label'])
                num_graphs += 1
                num_graphs_in_batch += 1
                node_offset += num_nodes_in_graph

            # Merge adjacency lists and information about incoming nodes:
            batch_num_incoming_edges = np.zeros([node_offset, params['n_edge_types']])
            for i in range(params['n_edge_types']):
                if len(batch_adjacency_lists[i]) > 0:
                    batch_adjacency_lists[i] = np.concatenate(batch_adjacency_lists[i])
                else:
                    batch_adjacency_lists[i] = np.zeros((0, 2), dtype=np.int32)

                for (v, n) in batch_num_incoming_edges_dicts[i].items():
                    batch_num_incoming_edges[v, i] = n

            batch_data = dict(num_graphs=num_graphs_in_batch,
                              node_features=np.array(batch_node_features),
                              target_values=np.array(batch_graph_target_values),
                              graph_nodes_list=np.array(batch_graph_nodes_list, dtype=np.int32),
                              num_incoming_edges=batch_num_incoming_edges,
                              adjacency_lists=batch_adjacency_lists)

            yield batch_data

    loss = 0
    accuracy = 0
    start_time = time.time()
    instances = 0
    for step, batch_data in enumerate(ThreadedIterator(minibatch_iterator(), max_queue_size=3)):
        instances += batch_data['num_graphs']
        feed_dict = {
            model.num_graphs: batch_data['num_graphs'],
            model.node_features: batch_data['node_features'],
            model.target_values: batch_data['target_values'],
            model.num_incoming_edges: batch_data['num_incoming_edges'],
            model.graph_nodes_list: batch_data['graph_nodes_list']
        }
        for i in range(params['n_edge_types']):
            feed_dict[model.adjacency_lists[i]] = batch_data['adjacency_lists'][i]
        del batch_data['adjacency_lists']
        if is_training:
            feed_dict[model.dropout_keep_prob] = params['dropout_keep_prob']
            fetch_list = [model.loss, model.accuracy, model.train_op]
        else:
            feed_dict[model.dropout_keep_prob] = 1.0
            fetch_list = [model.loss, model.accuracy]
        result = sess.run(fetch_list, feed_dict=feed_dict)
        loss += result[0] * batch_data['num_graphs']
        accuracy += result[1] * batch_data['num_graphs']

        if step % 100 == 0:
            print(instances, result[0])

    accuracy = accuracy / len(data)
    loss = loss / len(data)

    error_ratio = accuracy / chemical_accuracy[params["task_id"]]

    instance_per_sec = instances / (time.time() - start_time)
    print("loss: %s | error_ratio: %s | instances/sec: %s" % (loss, error_ratio, instance_per_sec))
    return instance_per_sec, loss, accuracy



def main():
    args = docopt(__doc__)
    params = make_params(args)
    data = get_data(args, params)
    update_output_path(args, params)

    np.random.seed(0)
    tf.set_random_seed(0)

    with tf.variable_scope(tf.get_variable_scope()):
        model = ChemGNN(params)

    sess = tf.Session()
    init_op = tf.group(tf.global_variables_initializer(),
                       tf.local_variables_initializer())
    sess.run(init_op)

    log_to_save = []
    total_time_start = time.time()
    total_time = 0
    val_acc = 0
    for epoch in range(1, 5000):
        log_entry = {}

        print('epoch', epoch, 'train ')
        train_instances_per_s, train_loss, train_acc = training_loop(
            sess, model, data['train'], params, True)
        if params['do_validation']:
            print('epoch', epoch, 'valid ',)
            val_instances_per_s, val_loss, val_acc = training_loop(
                sess, model, data['valid'], params, False)
        else:
            val_instances_per_s = val_loss = -1

        total_time = time.time() - total_time_start
        log_entry['epoch'] = epoch
        log_entry['time'] = total_time
        log_entry['train_instances_per_s'] = train_instances_per_s
        log_entry['train_loss'] = train_loss
        log_entry['train_acc'] = train_acc
        log_entry['val_instances_per_s'] = val_instances_per_s
        log_entry['val_loss'] = val_loss
        log_entry['val_acc'] = val_acc
        log_to_save.append(log_entry)
        with open(params['out'], 'w') as f:
            json.dump(log_to_save, f, indent=4)

if __name__ == "__main__":
    try:
        main()
    except:
        type, value, tb = sys.exc_info()
        traceback.print_exc()
        pdb.post_mortem(tb)
