import numpy as np
import numpy.random as npr
import gnumpy as gp
import StanfordTrees as st
import Word2Vec as w2v
import random as random
from time import clock
from sys import stdout as stdout

def simple_stb_test(tree_dir='./trees'):
    stb_data = st.SimpleLoad(tree_dir)
    return

if __name__ == '__main__':
    # Load tree data
    tree_dir = './trees'
    stb_data = st.SimpleLoad(tree_dir)
    max_lut_idx = max(stb_data['lut_keys'].values())

    # Get the lists of full train and test phrases
    tr_phrases = stb_data['train_full_phrases']
    te_phrases = stb_data['train_full_phrases']
    # Get the list of all word occurrences in the training phrases
    tr_words = []
    for phrase in tr_phrases:
        tr_words.extend(phrase)

    batch_count = 100
    batch_size = 256
    context_size = 5
    word_count = max_lut_idx + 1
    embed_dim = 300
    bias_dim = 150
    lam_l2 = 1e-6

    # Create a lookup table for word representations
    word_lut = w2v.LUTLayer(word_count, embed_dim)
    tanh_layer = w2v.TanhLayer(in_layer=word_lut)
    noise_layer = w2v.NoiseLayer(in_layer=tanh_layer, drop_rate=0.0, fuzz_scale=0.0)
    phrase_layer = w2v.CMLayer(key_count=len(tr_phrases), source_dim=embed_dim, bias_dim=bias_dim)

    # Create a full/softmax layer for classification
    class_layer = w2v.NSLayer(key_count=max_lut_idx, in_dim=(embed_dim+bias_dim))

    # Initialize params for the LUT and softmax classifier
    word_lut.init_params(0.05)
    class_layer.init_params(0.05)

    table_time = 0.0
    other_time = 0.0
    print("Processing batches:")
    t1 = clock()
    L = 0.0
    for b in range(batch_count):
        # Reset adagrad smoothing factors from time-to-time
        if ((b % 1000) == 0):
            word_lut.reset_moms()
            phrase_layer.reset_moms()
            class_layer.reset_moms()

        # Sample a batch of random anchor/context prediction pairs for
        # training a skip-gram model.
        [a_idx, p_idx, n_idx, phrase_idx] = \
            w2v.rand_pos_neg(tr_phrases, tr_words, batch_size, context_size, 8)

        tt = clock()
        # Feedforward through word look-up and phrase biasing/reweighting
        Xb = word_lut.feedforward(a_idx)
        Xp = phrase_layer.feedforward(Xb, phrase_idx)
        table_time += clock() - tt

        # Feedforward and backprop through prediction layer
        L += class_layer.feedforward(Xp, p_idx, n_idx)

        # Compute and display loss from time-to-time (for diagnostics)
        if ((b % 10) == 0):
            print("Batch {0:d}, loss {1:.4f}".format(b, (L / 10.0)))
            L = 0.0

        # Backprop through prediction layer
        dLdXp = class_layer.backprop()

        tt = clock()
        # Backprop through phrase biasing and reweighting
        dLdXb = phrase_layer.backprop(dLdXp)
        # Backprop through word look-up-table
        word_lut.backprop(dLdXb)
        table_time += clock() - tt

        # Update parameters based on the gradients for this batch
        tt = clock()
        #if ((b % 10) == 0):
        #    word_lut.l2_regularize(lam_l2)
        #    class_layer.l2_regularize(lam_l2)
        #    phrase_layer.l2_regularize(lam_l2)
        word_lut.apply_grads(learn_rate=2e-4, ada_smooth=1e-3)
        class_layer.apply_grads(learn_rate=2e-4, ada_smooth=1e-3)
        phrase_layer.apply_grads(learn_rate=2e-4, ada_smooth=1e-3)
        other_time += clock() - tt

    t2 = clock()
    e_time = t2 - t1
    print("Word count: {0:d}, word dim: {1:d}".format(word_count, embed_dim))
    print("elapsed time: {0:.4f}".format(e_time))
    print("table ff/bp time: {0:.4f}".format(table_time))
    print("class ff/bp time: {0:.4f}".format(class_layer.comp_time))
    print("updates time: {0:.4f}".format(other_time))
    print("Words per second: {0:.4f}".format((1.0*batch_count*batch_size /
        e_time)))



##############
# EYE BUFFER #
##############
