#!/usr/bin/env cython
# cython: boundscheck=False
# cython: wraparound=False
# cython: cdivision=True
# coding: utf-8
#
# Code in this file was written primarily by Philip Bachman, based on code
# made publically avilable under the license given below...
#
# Copyright (C) 2013 Radim Rehurek <me@radimrehurek.com>
# Licensed under the GNU LGPL v2.1 - http://www.gnu.org/licenses/lgpl.html
#

import cython
import numpy as np
cimport numpy as np

from libc.math cimport exp, log, sqrt
from libc.string cimport memset

cdef extern from "voidptr.h":
    void* PyCObject_AsVoidPtr(object obj)

from scipy.linalg import blas

ctypedef np.float32_t REAL_t
ctypedef np.uint32_t UI32_t
ctypedef np.int32_t I32_t

DEF MAX_SENTENCE_LEN = 10000

ctypedef void (*scopy_ptr) (const int *N, const float *X, const int *incX, float *Y, const int *incY) nogil
ctypedef void (*saxpy_ptr) (const int *N, const float *alpha, const float *X, const int *incX, float *Y, const int *incY) nogil
ctypedef float (*sdot_ptr) (const int *N, const float *X, const int *incX, const float *Y, const int *incY) nogil
ctypedef double (*dsdot_ptr) (const int *N, const float *X, const int *incX, const float *Y, const int *incY) nogil
ctypedef double (*snrm2_ptr) (const int *N, const float *X, const int *incX) nogil
ctypedef void (*sscal_ptr) (const int *N, const float *alpha, const float *X, const int *incX) nogil


ctypedef void (*cy_w2v_ff_bp_ptr) (
    const int sp_size, const UI32_t *sp_idx, const UI32_t *anc_keys,
    const int pn_size, const UI32_t *pn_keys, REAL_t *pn_sign,
    REAL_t *Wa, REAL_t *Wc, REAL_t *b, REAL_t *dWa, REAL_t *dWc, REAL_t *db,
    REAL_t *L, const int do_grad, const int vec_dim) nogil

ctypedef void (*cy_nsl_ff_bp_ptr) (
    const int sp_size, const UI32_t *sp_idx,
    const int pn_size, const UI32_t *pn_keys, REAL_t *pn_sign,
    REAL_t *X, REAL_t *W, REAL_t *b,
    REAL_t *dX, REAL_t *dW, REAL_t *db,
    REAL_t *L, const int do_grad, const int vec_dim) nogil

ctypedef void (*cy_acl_ff_bp_ptr) (
    const int sp_size, const UI32_t *sp_idx,
    const int pn_size, const UI32_t *pn_keys, REAL_t *pn_sign,
    REAL_t *X, REAL_t *W, REAL_t *b,
    REAL_t *dX, REAL_t *dW, REAL_t *db,
    REAL_t *L, const int do_grad, const int vec_dim) nogil

cdef scopy_ptr scopy=<scopy_ptr>PyCObject_AsVoidPtr(blas.scopy._cpointer)  # y = x
cdef saxpy_ptr saxpy=<saxpy_ptr>PyCObject_AsVoidPtr(blas.saxpy._cpointer)  # y += alpha * x
cdef sdot_ptr sdot=<sdot_ptr>PyCObject_AsVoidPtr(blas.sdot._cpointer)  # float = dot(x, y)
cdef dsdot_ptr dsdot=<dsdot_ptr>PyCObject_AsVoidPtr(blas.sdot._cpointer)  # double = dot(x, y)
cdef snrm2_ptr snrm2=<snrm2_ptr>PyCObject_AsVoidPtr(blas.snrm2._cpointer)  # sqrt(x^2)
cdef sscal_ptr sscal=<sscal_ptr>PyCObject_AsVoidPtr(blas.sscal._cpointer) # x = alpha * x

cdef cy_w2v_ff_bp_ptr cy_w2v_ff_bp
cdef cy_nsl_ff_bp_ptr cy_nsl_ff_bp
cdef cy_acl_ff_bp_ptr cy_acl_ff_bp

# define some useful constants
cdef int ONE = 1
cdef UI32_t MAX_HSM_KEY = <UI32_t>12345678
cdef REAL_t ONEF = <REAL_t>1.0
cdef REAL_t ADA_EPS = <REAL_t>0.001
cdef REAL_t ADA_RHO = <REAL_t>0.98

#############
# W2V_FF_BP #
#############

cdef void cy_w2v_ff_bp0(
    const int sp_size, const UI32_t *sp_idx, const UI32_t *anc_keys,
    const int pn_size, const UI32_t *pn_keys, REAL_t *pn_sign,
    REAL_t *Wa, REAL_t *Wc, REAL_t *b, REAL_t *dWa, REAL_t *dWc, REAL_t *db,
    REAL_t *L, const int do_grad, const int vec_dim) nogil:

    # declarations
    cdef long long row1, row2
    cdef REAL_t label, y, exp_pns_y, g
    cdef UI32_t a_key, c_key
    cdef int sp_i, i, j

    # update loop
    for sp_i in range(sp_size):
        i = <int>sp_idx[sp_i]
        a_key = anc_keys[i] # get the LUT key for the anchor word
        row1 = a_key * vec_dim # get the starting index of anchor word's row
        for j in range(pn_size):
            c_key = pn_keys[i*pn_size + j] # get the LUT key for the context word
            row2 = c_key * vec_dim # get the starting index of context word's row
            neg_label = -1.0 * pn_sign[i*pn_size + j]
            # compute prediction y as np.dot(a_vec, c_vec.T) + b[c_key]
            y = <REAL_t>dsdot(&vec_dim, &Wa[row1], &ONE, &Wc[row2], &ONE) + b[c_key]
            exp_pns_y = <REAL_t>exp(neg_label * y)
            L[0] = L[0] + log(1.0 + exp_pns_y) # add the loss on this a/c pair
            if (do_grad == 1):
                # Compute gradient and update parameter gradient accumulators
                g = neg_label * (exp_pns_y / (1.0 + exp_pns_y))
                saxpy(&vec_dim, &g, &Wa[row1], &ONE, &dWc[row2], &ONE)
                saxpy(&vec_dim, &g, &Wc[row2], &ONE, &dWa[row1], &ONE)
                db[c_key] = db[c_key] + g
    return

cdef void cy_w2v_ff_bp1(
    const int sp_size, const UI32_t *sp_idx, const UI32_t *anc_keys,
    const int pn_size, const UI32_t *pn_keys, REAL_t *pn_sign,
    REAL_t *Wa, REAL_t *Wc, REAL_t *b, REAL_t *dWa, REAL_t *dWc, REAL_t *db,
    REAL_t *L, const int do_grad, const int vec_dim) nogil:

    # declarations
    cdef long long row1, row2
    cdef REAL_t label, y, exp_pns_y, g
    cdef UI32_t a_key, c_key
    cdef int sp_i, i, j

    # update loop
    for sp_i in range(sp_size):
        i = <int>sp_idx[sp_i]
        a_key = anc_keys[i] # get the LUT key for the anchor word
        row1 = a_key * vec_dim # get the starting index of anchor word's row
        for j in range(pn_size):
            c_key = pn_keys[i*pn_size + j] # get the LUT key for the context word
            row2 = c_key * vec_dim # get the starting index of context word's row
            neg_label = -1.0 * pn_sign[i*pn_size + j]
            # compute prediction y as np.dot(a_vec, c_vec.T) + b[c_key]
            y = <REAL_t>sdot(&vec_dim, &Wa[row1], &ONE, &Wc[row2], &ONE) + b[c_key]
            exp_pns_y = <REAL_t>exp(neg_label * y)
            L[0] = L[0] + log(1.0 + exp_pns_y) # add the loss on this a/c pair
            if (do_grad == 1):
                # Compute gradient and update parameter gradient accumulators
                g = neg_label * (exp_pns_y / (1.0 + exp_pns_y))
                saxpy(&vec_dim, &g, &Wa[row1], &ONE, &dWc[row2], &ONE)
                saxpy(&vec_dim, &g, &Wc[row2], &ONE, &dWa[row1], &ONE)
                db[c_key] = db[c_key] + g
    return

def w2v_ff_bp_pyx(sp_idx_p, anc_keys_p, pn_keys_p, pn_sign_p, Wa_p, Wc_p, b_p,
                  dWa_p, dWc_p, db_p, L_p, do_grad_p):
    # Define and cast minibatch problem parameters
    cdef int sp_size = <int>sp_idx_p.shape[0]
    cdef int pn_size = <int>pn_keys_p.shape[1]
    cdef int do_grad = <int>do_grad_p
    cdef int vec_dim = <int>Wa_p.shape[1]
    cdef UI32_t *sp_idx = <UI32_t *>(np.PyArray_DATA(sp_idx_p))
    cdef UI32_t *anc_keys = <UI32_t *>(np.PyArray_DATA(anc_keys_p))
    cdef UI32_t *pn_keys = <UI32_t *>(np.PyArray_DATA(pn_keys_p))
    cdef REAL_t *pn_sign = <REAL_t *>(np.PyArray_DATA(pn_sign_p))
    cdef REAL_t *Wa = <REAL_t *>(np.PyArray_DATA(Wa_p))
    cdef REAL_t *Wc = <REAL_t *>(np.PyArray_DATA(Wc_p))
    cdef REAL_t *b = <REAL_t *>(np.PyArray_DATA(b_p))
    cdef REAL_t *dWa = <REAL_t *>(np.PyArray_DATA(dWa_p))
    cdef REAL_t *dWc = <REAL_t *>(np.PyArray_DATA(dWc_p))
    cdef REAL_t *db = <REAL_t *>(np.PyArray_DATA(db_p))
    cdef REAL_t *L = <REAL_t *>(np.PyArray_DATA(L_p))

    with nogil:
        cy_w2v_ff_bp(sp_size, sp_idx, anc_keys, pn_size, pn_keys, pn_sign,
                     Wa, Wc, b, dWa, dWc, db, L, do_grad, vec_dim)
    return


#############
# NSL_FF_BP #
################################################################################
# NOTE: This function is used by both the NSLayer and HSMLayer classes, i.e.   #
#       the classes implementing negative sampling and hierarchial softmax.    #
#       This "overloading" is reasonable because negative sampling and         #
#       hierarchical softmax are both based on combining an "anchor" vector,   #
#       fed-forward by some previous layer in the model, with a sequence of    #
#       binary logistic regressions, whose parameters are to-be-learned by the #
#       current NSLayer/HSMLayer.                                              #
#                                                                              #
#       Parameters passed to nsl_ff_bp_pyx:                                    #
#         sp_idx_p: Numpy array of int32 keys into the rows of pn_keys_p,      #
#                   pn_sign_p, X_p and dX_p. This param is used by the calling #
#                   Python code to divvy up subproblems for multithreading.    #
#         pn_keys_p: Numpy matrix of int32 keys into W_p, b_p, dW_p, and db_p. #
#                    Each row of pn_keys gives keys for a set of +/- targets   #
#                    generated for either training via negative sampling or a  #
#                    hierarchical softmax code vector sequence.                #
#         pn_sign_p: Numpy matrix of float32 in {+1, -1}, giving the desired   #
#                    outcome of each prediction to-be-performed.               #
#         X_p: Numpy matrix of float32 "anchor" vectors, to be used as inputs  #
#              to each prediction. *same number of rows as pn_keys_p/pn_sign_p #
#         W_p: Numpy matrix of float32 params for this NSLayer/HSMLayer.       #
#              *same number of columns as X_p                                  #
#         b_p: Numpy array of float32 giving a bias for each row of W_p.       #
#         dX_p, dW_p, db_p: np.float32 gradient accumulators for X_p/W_p/b_p   #
#         L_p: Numpy array with one element -- to accumulate loss information  #
#         do_grad_p: int in {0, 1}. if it's 0, then we will only compute loss  #
#                    and grad arrays will be left untouched. otherwise, the    #
#                    grad arrays will be modified with the new grad info.      #
#                                                                              #
#                                                                              #
#       1. When used by NSLayer, pn_keys gives the (NSLayer) LUT keys for the  #
#       various positive/negative prediction targets to train on for a given   #
#       anchor vector. The desired outcome of each prediction is given by the  #
#       corresponding entries of pn_sign.                                      #
#                                                                              #
#       2. When used by HSMLayer, pn_keys gives the (HSMLayer) LUT keys for    #
#       each code vector used by hierarchical softmax to represent a given     #
#       prediction target for a given anchor vector. For each given anchor     #
#       vector / prediction target pair multiple code vectors are required,    #
#       but each anchor/target pair may use different numbers of code vectors. #
#       When processing the row of pn_keys which gives the code vector keys    #
#       for a given anchor/target pair, a huge key indicates that no more code #
#       vectors need be processed for this pair. For each valid code vec key   #
#       there is an associated target class in {+1, -1}, stored in the same    #
#       element in pn_sign as the code vector key was in pn_keys.              #
#                                                                              #
################################################################################

cdef void cy_nsl_ff_bp0(
    const int sp_size, const UI32_t *sp_idx,
    const int pn_size, const UI32_t *pn_keys, REAL_t *pn_sign,
    REAL_t *X, REAL_t *W, REAL_t *b,
    REAL_t *dX, REAL_t *dW, REAL_t *db,
    REAL_t *L, const int do_grad, const int vec_dim) nogil:

    # declarations
    cdef long long row1, row2
    cdef REAL_t label, y, exp_pns_y, g
    cdef UI32_t X_key, W_key
    cdef int sp_i, i, j

    # update loop
    for sp_i in range(sp_size):
        X_key = sp_idx[sp_i]
        row1 = X_key * vec_dim # get the starting index of input row (in X)
        for j in range(pn_size):
            W_key = pn_keys[X_key*pn_size + j]
            if (W_key < MAX_HSM_KEY):
                row2 = W_key * vec_dim # get the starting index of target row (in W)
                neg_label = -1.0 * pn_sign[X_key*pn_size + j] # minus the label
                # compute prediction y as np.dot(X[X_key], W[W_key].T) + b[W_key]
                y = <REAL_t>dsdot(&vec_dim, &X[row1], &ONE, &W[row2], &ONE) + b[W_key]
                exp_pns_y = <REAL_t>exp(neg_label * y) # this is used for loss/grad
                L[X_key*pn_size + j] = log(1.0 + exp_pns_y) # record the loss
                if (do_grad == 1):
                    # Compute gradient and update gradient accumulators
                    g = neg_label * (exp_pns_y / (1.0 + exp_pns_y))
                    saxpy(&vec_dim, &g, &X[row1], &ONE, &dW[row2], &ONE)
                    saxpy(&vec_dim, &g, &W[row2], &ONE, &dX[row1], &ONE)
                    db[W_key] = db[W_key] + g
    return


cdef void cy_nsl_ff_bp1(
    const int sp_size, const UI32_t *sp_idx,
    const int pn_size, const UI32_t *pn_keys, REAL_t *pn_sign,
    REAL_t *X, REAL_t *W, REAL_t *b,
    REAL_t *dX, REAL_t *dW, REAL_t *db,
    REAL_t *L, const int do_grad, const int vec_dim) nogil:

    # declarations
    cdef long long row1, row2
    cdef REAL_t label, y, exp_pns_y, g
    cdef UI32_t X_key, W_key
    cdef int sp_i, i, j

    # update loop
    for sp_i in range(sp_size):
        X_key = sp_idx[sp_i]
        row1 = X_key * vec_dim # get the starting index of input row (in X)
        for j in range(pn_size):
            W_key = pn_keys[X_key*pn_size + j]
            if (W_key < MAX_HSM_KEY):
                row2 = W_key * vec_dim # get the starting index of target row (in W)
                neg_label = -1.0 * pn_sign[X_key*pn_size + j] # minus the label
                # compute prediction y as np.dot(X[X_key], W[W_key].T) + b[W_key]
                y = <REAL_t>sdot(&vec_dim, &X[row1], &ONE, &W[row2], &ONE) + b[W_key]
                exp_pns_y = <REAL_t>exp(neg_label * y) # this is used for loss/grad
                L[X_key*pn_size + j] = log(1.0 + exp_pns_y) # record the loss
                if (do_grad == 1):
                    # Compute gradient and update gradient accumulators
                    g = neg_label * (exp_pns_y / (1.0 + exp_pns_y))
                    saxpy(&vec_dim, &g, &X[row1], &ONE, &dW[row2], &ONE)
                    saxpy(&vec_dim, &g, &W[row2], &ONE, &dX[row1], &ONE)
                    db[W_key] = db[W_key] + g
    return

def nsl_ff_bp_pyx(sp_idx_p, pn_keys_p, pn_sign_p, X_p, W_p, b_p,
                  dX_p, dW_p, db_p, L_p, do_grad_p):
    # Define and cast minibatch problem parameters
    cdef int sp_size = <int>sp_idx_p.shape[0]
    cdef int pn_size = <int>pn_keys_p.shape[1]
    cdef int do_grad = <int>do_grad_p
    cdef int vec_dim = <int>W_p.shape[1]
    cdef UI32_t *sp_idx = <UI32_t *>(np.PyArray_DATA(sp_idx_p))
    cdef UI32_t *pn_keys = <UI32_t *>(np.PyArray_DATA(pn_keys_p))
    cdef REAL_t *pn_sign = <REAL_t *>(np.PyArray_DATA(pn_sign_p))
    cdef REAL_t *X = <REAL_t *>(np.PyArray_DATA(X_p))
    cdef REAL_t *W = <REAL_t *>(np.PyArray_DATA(W_p))
    cdef REAL_t *b = <REAL_t *>(np.PyArray_DATA(b_p))
    cdef REAL_t *dX = <REAL_t *>(np.PyArray_DATA(dX_p))
    cdef REAL_t *dW = <REAL_t *>(np.PyArray_DATA(dW_p))
    cdef REAL_t *db = <REAL_t *>(np.PyArray_DATA(db_p))
    cdef REAL_t *L = <REAL_t *>(np.PyArray_DATA(L_p))

    with nogil:
        cy_nsl_ff_bp(sp_size, sp_idx, pn_size, pn_keys, pn_sign,
                     X, W, b, dX, dW, db, L, do_grad, vec_dim)
    return


################################
# AUTO-CONTRASTIVE LAYER FF/BP #
################################


cdef void cy_acl_ff_bp0(
    const int sp_size, const UI32_t *sp_idx,
    const int pn_size, const UI32_t *pn_keys, REAL_t *pn_sign,
    REAL_t *X, REAL_t *W, REAL_t *b,
    REAL_t *dX, REAL_t *dW, REAL_t *db,
    REAL_t *L, const int do_grad, const int vec_dim) nogil:

    # declarations
    cdef long long row1, r_p, r_n
    cdef REAL_t y_p, b_p, f_p, y_n, b_n, f_n, a, k, c, denom, \
            g_yp, g_bp, g_yn, g_bn
    cdef UI32_t Xk, Wk_p, Wk_n
    cdef int sp_i, i, j

    a = <REAL_t> 1.0
    k = <REAL_t> 1.0
    c = <REAL_t> 0.1
    # update loop
    for sp_i in range(sp_size):
        Xk = sp_idx[sp_i]
        row1 = Xk * vec_dim
        for j in range(pn_size):
            if (j == 0):
                # get the LUT key for the pos vec
                Wk_p = pn_keys[Xk*pn_size + j]
                # get pointer for start of the pos vec
                r_p = Wk_p * vec_dim
                # compute dot-prod between input and the pos vec
                y_p = <REAL_t>dsdot(&vec_dim, &X[row1], &ONE, &W[r_p], &ONE)
                # get the bias for the pos vec
                b_p = b[Wk_p]
                # compute expy output for the pos vec
                f_p = <REAL_t>exp(a*y_p + b_p)
            else:
                # get the LUT key for the neg vec
                Wk_n = pn_keys[Xk*pn_size + j]
                # get pointer for start of the neg vec
                r_n  = Wk_n * vec_dim
                # compute dot-prod between input and the neg vec
                y_n = <REAL_t>dsdot(&vec_dim, &X[row1], &ONE, &W[r_n], &ONE)
                # get the bias for the neg vec
                b_n = b[Wk_n]
                # compute expy output for the neg vec
                f_n = <REAL_t>(k * exp(a*y_n + b_n))
                # compute some terms
                denom = f_p + f_n + c
                L[Xk*pn_size + j] = -log(f_p / denom)
                if (do_grad == 1):
                    # compute gradients for the pos/neg vec terms
                    g_yp = -(a * (c + f_n)) / denom
                    g_yn = (a * f_n) / denom
                    # update gradient accumlators for input side
                    saxpy(&vec_dim, &g_yp, &W[r_p], &ONE, &dX[row1], &ONE)
                    saxpy(&vec_dim, &g_yn, &W[r_n], &ONE, &dX[row1], &ONE)
                    # update gradient accumlators for prediction side
                    saxpy(&vec_dim, &g_yp, &X[row1], &ONE, &dW[r_p], &ONE)
                    saxpy(&vec_dim, &g_yn, &X[row1], &ONE, &dW[r_n], &ONE)
                    # compute gradients and updates for bias terms
                    g_bp = -(c + f_n) / denom
                    g_bn = f_n / denom
                    db[Wk_p] = db[Wk_p] + g_bp
                    db[Wk_n] = db[Wk_n] + g_bn
    return


cdef void cy_acl_ff_bp1(
    const int sp_size, const UI32_t *sp_idx,
    const int pn_size, const UI32_t *pn_keys, REAL_t *pn_sign,
    REAL_t *X, REAL_t *W, REAL_t *b,
    REAL_t *dX, REAL_t *dW, REAL_t *db,
    REAL_t *L, const int do_grad, const int vec_dim) nogil:

    # declarations
    cdef long long row1, r_p, r_n
    cdef REAL_t y_p, b_p, f_p, y_n, b_n, f_n, a, k, c, denom, \
            g_yp, g_bp, g_yn, g_bn
    cdef UI32_t Xk, Wk_p, Wk_n
    cdef int sp_i, i, j

    a = <REAL_t> 1.0
    k = <REAL_t> 1.0
    c = <REAL_t> 0.1

    # NOTE: this function basically minimizes something like:
    #
    #     /              exp(a*y_p + b_p)             \
    # - ln| ----------------------------------------- |
    #     \ c + exp(a*y_p + b_p) + k*exp(a*y_n + b_n) /
    # 

    # update loop
    for sp_i in range(sp_size):
        Xk = sp_idx[sp_i]
        row1 = Xk * vec_dim
        for j in range(pn_size):
            if (j == 0):
                # get the LUT key for the pos vec
                Wk_p = pn_keys[Xk*pn_size + j]
                # get pointer for start of the pos vec
                r_p = Wk_p * vec_dim
                # compute dot-prod between input and the pos vec
                y_p = <REAL_t>sdot(&vec_dim, &X[row1], &ONE, &W[r_p], &ONE)
                # get the bias for the pos vec
                b_p = b[Wk_p]
                # compute expy output for the pos vec
                f_p = <REAL_t>exp(a*y_p + b_p)
            else:
                # get the LUT key for the neg vec
                Wk_n = pn_keys[Xk*pn_size + j]
                # get pointer for start of the neg vec
                r_n  = Wk_n * vec_dim
                # compute dot-prod between input and the neg vec
                y_n = <REAL_t>sdot(&vec_dim, &X[row1], &ONE, &W[r_n], &ONE)
                # get the bias for the neg vec
                b_n = b[Wk_n]
                # compute expy output for the neg vec
                f_n = <REAL_t>(k * exp(a*y_n + b_n))
                # compute some terms
                denom = f_p + f_n + c
                L[Xk*pn_size + j] = -log(f_p / denom)
                if (do_grad == 1):
                    # compute gradients for the pos/neg vec terms
                    g_yp = -(a * (c + f_n)) / denom
                    g_yn = (a * f_n) / denom
                    # update gradient accumlators for input side
                    saxpy(&vec_dim, &g_yp, &W[r_p], &ONE, &dX[row1], &ONE)
                    saxpy(&vec_dim, &g_yn, &W[r_n], &ONE, &dX[row1], &ONE)
                    # update gradient accumlators for prediction side
                    saxpy(&vec_dim, &g_yp, &X[row1], &ONE, &dW[r_p], &ONE)
                    saxpy(&vec_dim, &g_yn, &X[row1], &ONE, &dW[r_n], &ONE)
                    # compute gradients and updates for bias terms
                    g_bp = -(c + f_n) / denom
                    g_bn = f_n / denom
                    db[Wk_p] = db[Wk_p] + g_bp
                    db[Wk_n] = db[Wk_n] + g_bn
    return

def acl_ff_bp_pyx(sp_idx_p, pn_keys_p, pn_sign_p, X_p, W_p, b_p,
                  dX_p, dW_p, db_p, L_p, do_grad_p):
    # Define and cast minibatch problem parameters
    cdef int sp_size = <int>sp_idx_p.shape[0]
    cdef int pn_size = <int>pn_keys_p.shape[1]
    cdef int do_grad = <int>do_grad_p
    cdef int vec_dim = <int>W_p.shape[1]
    cdef UI32_t *sp_idx = <UI32_t *>(np.PyArray_DATA(sp_idx_p))
    cdef UI32_t *pn_keys = <UI32_t *>(np.PyArray_DATA(pn_keys_p))
    cdef REAL_t *pn_sign = <REAL_t *>(np.PyArray_DATA(pn_sign_p))
    cdef REAL_t *X = <REAL_t *>(np.PyArray_DATA(X_p))
    cdef REAL_t *W = <REAL_t *>(np.PyArray_DATA(W_p))
    cdef REAL_t *b = <REAL_t *>(np.PyArray_DATA(b_p))
    cdef REAL_t *dX = <REAL_t *>(np.PyArray_DATA(dX_p))
    cdef REAL_t *dW = <REAL_t *>(np.PyArray_DATA(dW_p))
    cdef REAL_t *db = <REAL_t *>(np.PyArray_DATA(db_p))
    cdef REAL_t *L = <REAL_t *>(np.PyArray_DATA(L_p))

    with nogil:
        cy_acl_ff_bp(sp_size, sp_idx, pn_size, pn_keys, pn_sign,
                     X, W, b, dX, dW, db, L, do_grad, vec_dim)
    return

################
# AG_UPDATE_2D #
################

cdef void cy_ag_update_2d(
    const int sp_size, const UI32_t *sp_idx, const UI32_t *row_idx,
    REAL_t *W, REAL_t *dW, REAL_t *mW, REAL_t alpha,
    const int vec_dim) nogil:

    # declarations
    cdef long long row_ptr, v_i
    cdef int sp_i, vec_bytes
    cdef UI32_t i, row_key

    # update loop
    vec_bytes = cython.sizeof(REAL_t) * vec_dim
    for sp_i in range(sp_size):
        i = sp_idx[sp_i]
        row_key = row_idx[i]
        row_ptr = row_key * vec_dim
        for v_i in range(vec_dim):
            mW[row_ptr + v_i] = (ADA_RHO * mW[row_ptr + v_i]) + \
                    ((1 - ADA_RHO) * dW[row_ptr + v_i] * dW[row_ptr + v_i])
            W[row_ptr + v_i] -= alpha * \
                    (dW[row_ptr + v_i] / (sqrt(mW[row_ptr + v_i]) + ADA_EPS))
            dW[row_ptr + v_i] = 0.0
    return

def ag_update_2d_pyx(sp_idx_p, row_idx_p, W_p, dW_p, mW_p, alpha_p):
    # Define and cast minibatch problem parameters
    cdef int sp_size = <int>sp_idx_p.shape[0]
    cdef int vec_dim = <int>W_p.shape[1]
    cdef UI32_t *sp_idx = <UI32_t *>(np.PyArray_DATA(sp_idx_p))
    cdef UI32_t *row_idx = <UI32_t *>(np.PyArray_DATA(row_idx_p))
    cdef REAL_t *W = <REAL_t *>(np.PyArray_DATA(W_p))
    cdef REAL_t *dW = <REAL_t *>(np.PyArray_DATA(dW_p))
    cdef REAL_t *mW = <REAL_t *>(np.PyArray_DATA(mW_p))
    cdef REAL_t alpha = <REAL_t>alpha_p

    with nogil:
        cy_ag_update_2d(sp_size, sp_idx, row_idx, W, dW, mW, alpha, vec_dim)
    return

################
# AG_UPDATE_1D #
################

cdef void cy_ag_update_1d(
    const int sp_size, const UI32_t *sp_idx, const UI32_t *row_idx, REAL_t *W,
    REAL_t *dW, REAL_t *mW, REAL_t alpha) nogil:

    # declarations
    cdef int sp_i
    cdef UI32_t i, row_key

    # update loop
    for sp_i in range(sp_size):
        i = sp_idx[sp_i]
        row_key = row_idx[i]
        mW[row_key] = (ADA_RHO * mW[row_key]) + \
                ((1 - ADA_RHO) * dW[row_key] * dW[row_key])
        W[row_key] -= alpha * (dW[row_key] / (sqrt(mW[row_key]) + ADA_EPS))
        dW[row_key] = 0.0
    return

def ag_update_1d_pyx(sp_idx_p, row_idx_p, W_p, dW_p, mW_p, alpha_p):
    # Define and cast minibatch problem parameters
    cdef int sp_size = <int>sp_idx_p.shape[0]
    cdef UI32_t *sp_idx = <UI32_t *>(np.PyArray_DATA(sp_idx_p))
    cdef UI32_t *row_idx = <UI32_t *>(np.PyArray_DATA(row_idx_p))
    cdef REAL_t *W = <REAL_t *>(np.PyArray_DATA(W_p))
    cdef REAL_t *dW = <REAL_t *>(np.PyArray_DATA(dW_p))
    cdef REAL_t *mW = <REAL_t *>(np.PyArray_DATA(mW_p))
    cdef REAL_t alpha = <REAL_t>alpha_p

    with nogil:
        cy_ag_update_1d(sp_size, sp_idx, row_idx, W, dW, mW, alpha)
    return

##########
# LUT_BP #
##########

cdef void cy_lut_bp(
    const int sp_size, const UI32_t *sp_idx, const UI32_t *row_idx,
    REAL_t *dLdY, REAL_t *dW, const int vec_dim) nogil:

    # declarations
    cdef long long row1, row2
    cdef int sp_i
    cdef UI32_t i, j

    # update loop
    for sp_i in range(sp_size):
        i = sp_idx[sp_i] # row key for dLdY
        j = row_idx[i] # row key for dW
        row1 = i * vec_dim
        row2 = j * vec_dim
        saxpy(&vec_dim, &ONEF, &dLdY[row1], &ONE, &dW[row2], &ONE)
    return

def lut_bp_pyx(sp_idx_p, row_idx_p, dLdY_p, dW_p):
    # Define and cast minibatch problem parameters
    cdef int sp_size = <int>sp_idx_p.shape[0]
    cdef int vec_dim = <int>dLdY_p.shape[1]
    cdef UI32_t *sp_idx = <UI32_t *>(np.PyArray_DATA(sp_idx_p))
    cdef UI32_t *row_idx = <UI32_t *>(np.PyArray_DATA(row_idx_p))
    cdef REAL_t *dLdY = <REAL_t *>(np.PyArray_DATA(dLdY_p))
    cdef REAL_t *dW = <REAL_t *>(np.PyArray_DATA(dW_p))

    with nogil:
        cy_lut_bp(sp_size, sp_idx, row_idx, dLdY, dW, vec_dim)
    return

###############
# INIT, INNIT #
###############

def init():
    """
    Bleep bloop: computer compute.
    """
    global cy_w2v_ff_bp
    global cy_nsl_ff_bp
    global cy_acl_ff_bp

    cdef float *x = [<float>10.0]
    cdef float *y = [<float>0.01]
    cdef float expected = <float>0.1
    cdef int size = 1
    cdef double d_res
    cdef float *p_res

    # check whether sdot returns double or float
    d_res = dsdot(&size, x, &ONE, y, &ONE)
    p_res = <float *>&d_res
    if (abs(d_res - expected) < 0.0001):
        cy_w2v_ff_bp = cy_w2v_ff_bp0
        cy_nsl_ff_bp = cy_nsl_ff_bp0
        cy_acl_ff_bp = cy_acl_ff_bp0
        return 0  # double
    elif (abs(p_res[0] - expected) < 0.0001):
        cy_w2v_ff_bp = cy_w2v_ff_bp1
        cy_nsl_ff_bp = cy_nsl_ff_bp1
        cy_acl_ff_bp = cy_acl_ff_bp1
        return 1  # float
    else:
        # dsdot returns neither single nor double precision!?
        assert False
        return 2

DO_INIT = init()  # initialize the module
