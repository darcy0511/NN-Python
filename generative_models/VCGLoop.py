################################################################################
# Code for managing and training a Variational Collaborative Generative Loop.  #
#                                                                              #
# Note: This is ongoing research and very much in flux.                        #
################################################################################

# basic python
import numpy as np
import numpy.random as npr
from collections import OrderedDict

# theano business
import theano
import theano.tensor as T
#from theano.tensor.shared_randomstreams import RandomStreams as RandStream
from theano.sandbox.cuda.rng_curand import CURAND_RandomStreams as RandStream

# phil's sweetness
from NetLayers import HiddenLayer, DiscLayer, safe_log, softplus_actfun
from GenNet import projected_moments

#################
# FOR PROFILING #
#################
#from theano import ProfileMode
#profmode = theano.ProfileMode(optimizer='fast_run', linker=theano.gof.OpWiseCLinker())

#############################
# SOME HANDY LOSS FUNCTIONS #
#############################

def logreg_loss(Y, class_sign):
    """
    Simple binomial deviance (i.e. logistic regression) loss.

    This assumes that all predictions in Y have the same target class, which
    is indicated by class_sign, which should be in {-1, +1}. Note: this does
    not "normalize" for the number of predictions in Y.
    """
    loss = T.sum(softplus_actfun(-class_sign * Y))
    return loss

def ns_nce_pos(f, k=1.0):
    """
    Negative-sampling noise contrastive estimation, for target distribution.
    """
    loss = T.sum(T.log(1.0 + k*T.exp(-f)))
    return loss

def ns_nce_neg(f, k=1.0):
    """
    Negative-sampling noise contrastive estimation, for base distribution.
    """
    loss = T.sum(f + T.log(1.0 + k*T.exp(-f)))
    return loss

def lsq_loss(Yh, Yt=0.0):
    """
    Least-squares loss for predictions in Yh, given target Yt.
    """
    loss = T.sum((Yh - Yt)**2.0)
    return loss

def hinge_loss(Yh, Yt=0.0):
    """
    Unilateral hinge loss for Yh, given target Yt.
    """
    residual = Yt - Yh
    loss = T.sum((residual * (residual > 0.0)))
    return loss

def ulh_loss(Yh, Yt=0.0, delta=0.5):
    """
    Unilateral Huberized least-squares loss for Yh, given target Yt.
    """
    residual = Yt - Yh
    quad_loss = residual**2.0
    line_loss = (2.0 * delta * abs(residual)) - delta**2.0
    # Construct mask for quadratic loss region
    quad_mask = (abs(residual) < delta) * (residual > 0.0)
    # Construct mask for linear loss region
    line_mask = (abs(residual) >= delta) * (residual > 0.0)
    # Combine the quadratic and linear losses
    loss = T.sum((quad_loss * quad_mask) + (line_loss * line_mask))
    return loss

def cat_entropy(p):
    """
    Compute the entropy of (row-wise) categorical distributions in p.
    """
    row_ents = -T.sum((p * safe_log(p)), axis=1, keepdims=True)
    return row_ents

def cat_prior_dir(p, alpha=0.1):
    """
    Log probability under a dirichlet prior, with dirichlet parameter alpha.
    """
    log_prob = T.sum((1.0 - alpha) * safe_log(p))
    return log_prob

def cat_prior_ent(p, ent_weight=1.0):
    """
    Log probability under an "entropy-type" prior, with some "weight".
    """
    log_prob = -cat_entropy * ent_weight
    return log_prob

def binarize_data(X):
    """
    Make a sample of bernoulli variables with probabilities given by X.
    """
    X_shape = X.shape
    probs = npr.rand(*X_shape)
    X_binary = 1.0 * (probs < X)
    return X_binary.astype(theano.config.floatX)

def sample_masks(X, drop_prob=0.3):
    """
    Sample a binary mask to apply to the matrix X, with rate mask_prob.
    """
    probs = npr.rand(*X.shape)
    mask = 1.0 * (probs > drop_prob)
    return mask.astype(theano.config.floatX)

def sample_patch_masks(X, im_shape, patch_shape):
    """
    Sample a random patch mask for each image in X.
    """
    obs_count = X.shape[0]
    rs = patch_shape[0]
    cs = patch_shape[1]
    off_row = npr.randint(1,high=(im_shape[0]-rs-1), size=(obs_count,))
    off_col = npr.randint(1,high=(im_shape[1]-cs-1), size=(obs_count,))
    dummy = np.zeros(im_shape)
    mask = np.zeros(X.shape)
    for i in range(obs_count):
        dummy = (0.0 * dummy) + 1.0
        dummy[off_row[i]:(off_row[i]+rs), off_col[i]:(off_col[i]+cs)] = 0.0
        mask[i,:] = dummy.ravel()
    return mask.astype(theano.config.floatX)

class VCGChain(object):
    """
    Controller for training a VAE using guidance from a classifier.

    The generator must be an instance of the GenNet class implemented in
    "GenNet.py". The discriminator must be an instance of the PeaNet class,
    as implemented in "PeaNet.py". The inferencer must be an instance of the
    InfNet class implemented in "InfNet.py".

    Parameters:
        rng: numpy.random.RandomState (for reproducibility)
        Xd: symbolic var for providing points for launching the Markov Chain
        Xt: symbolic var for providing samples from the target distribution
        i_net: The InfNet instance that will serve as the inferencer
        g_net: The GenNet instance that will serve as the generator
        d_net: The PeaNet instance that will serve as the discriminator
        chain_len: number of steps to unroll the VAE Markov Chain
        data_dim: dimension of the generated data
        prior_dim: dimension of the model prior
        params: a dict of parameters for controlling various costs
            lam_l2d: regularization on squared discriminator output
            mom_mix_rate: rate for updates to the running moment estimates
                          for the distribution generated by g_net
            mom_match_weight: weight for the "moment matching" cost
            mom_match_proj: projection matrix for reduced-dim mom matching
            target_mean: first-order moment to try and match with g_net
            target_cov: second-order moment to try and match with g_net
    """
    def __init__(self, rng=None, Xd=None, Xc=None, Xm=None, Xt=None, \
                 i_net=None, g_net=None, d_net=None, chain_len=None, \
                 data_dim=None, prior_dim=None, params=None):
        # Do some stuff!
        self.rng = RandStream(rng.randint(100000))
        self.data_dim = data_dim
        self.prior_dim = prior_dim

        # symbolic var for inputting samples for initializing the VAE chain
        self.Xd = Xd
        # symbolic var for masking subsets of the state variables
        self.Xm = Xm
        # symbolic var for controlling subsets of the state variables
        self.Xc = Xc
        # symbolic var for inputting samples from the target distribution
        self.Xt = Xt
        # integer number of times to cycle the VAE loop
        self.chain_len = chain_len
        # symbolic matrix of indices for data inputs
        self.It = T.arange(self.Xt.shape[0])
        # symbolic matrix of indices for noise inputs
        self.Id = T.arange(self.chain_len * self.Xd.shape[0]) + self.Xt.shape[0]

        # get a clone of the desired VAE, for easy access
        self.GIP = GIPair(rng=rng, Xd=self.Xd, Xc=self.Xc, Xm=self.Xm, \
                g_net=g_net, i_net=i_net, data_dim=self.data_dim, \
                prior_dim=self.prior_dim, params=None, shared_param_dicts=None)
        self.IN = self.GIP.IN
        self.GN = self.GIP.GN
        # self-loop some clones of the main VAE into a chain
        self.IN_chain = []
        self.GN_chain = []
        self.Xg_chain = []
        _Xd = self.Xd
        for i in range(self.chain_len):
            if (i == 0):
                # start the chain with data provided by user
                _IN = self.IN.shared_param_clone(rng=rng, Xd=_Xd, \
                        Xc=self.Xc, Xm=self.Xm)
                _GN = self.GN.shared_param_clone(rng=rng, Xp=_IN.output)
            else:
                # continue the chain with samples from previous VAE
                _IN = self.IN.shared_param_clone(rng=rng, Xd=_Xd, \
                        Xc=self.Xc, Xm=self.Xm)
                _GN = self.GN.shared_param_clone(rng=rng, Xp=_IN.output)
            _Xd = _GN.output
            self.IN_chain.append(_IN)
            self.GN_chain.append(_GN)
            self.Xg_chain.append(_Xd)
        #Xg_stack = T.vertical_stack(*self.Xg_chain)
        #self.Xg = Xg_stack + (0.1 * self.rng.normal(size=Xg_stack.shape, avg=0.0, \
        #        std=1.0, dtype=theano.config.floatX))

        # make a clone of the desired discriminator network, which will try
        # to discriminate between samples from the training data and samples
        # generated by the self-looped VAE chain.
        self.DN = d_net.shared_param_clone(rng=rng, \
                Xd=T.vertical_stack(self.Xt, *self.Xg_chain))

        zero_ary = np.zeros((1,)).astype(theano.config.floatX)
        # init shared var for weighting nll of data given posterior sample
        self.lam_chain_nll = theano.shared(value=zero_ary, name='vcg_lam_chain_nll')
        self.set_lam_chain_nll(lam_chain_nll=1.0)
        # init shared var for weighting posterior KL-div from prior
        self.lam_chain_kld = theano.shared(value=zero_ary, name='vcg_lam_chain_kld')
        self.set_lam_chain_kld(lam_chain_kld=1.0)
        # init shared var for weighting chain diffusion rate (a.k.a. velocity)
        self.lam_chain_vel = theano.shared(value=zero_ary, name='vcg_lam_chain_vel')
        self.set_lam_chain_vel(lam_chain_vel=1.0)
        # init shared var for weighting nll of data given posterior sample
        self.lam_mask_nll = theano.shared(value=zero_ary, name='vcg_lam_mask_nll')
        self.set_lam_mask_nll(lam_mask_nll=0.0)
        # init shared var for weighting posterior KL-div from prior
        self.lam_mask_kld = theano.shared(value=zero_ary, name='vcg_lam_mask_kld')
        self.set_lam_mask_kld(lam_mask_kld=0.0)
        # init shared var for controlling l2 regularization on params
        self.lam_l2w = theano.shared(value=zero_ary, name='vcg_lam_l2w')
        self.set_lam_l2w(lam_l2w=1e-4)
        # shared var learning rate for generator and discriminator
        self.lr_dn = theano.shared(value=zero_ary, name='vcg_lr_dn')
        self.lr_gn = theano.shared(value=zero_ary, name='vcg_lr_gn')
        self.lr_in = theano.shared(value=zero_ary, name='vcg_lr_in')
        # shared var momentum parameters for generator and discriminator
        self.mo_dn = theano.shared(value=zero_ary, name='vcg_mo_dn')
        self.mo_gn = theano.shared(value=zero_ary, name='vcg_mo_gn')
        self.mo_in = theano.shared(value=zero_ary, name='vcg_mo_in')
        # shared var weights for adversarial classification objective
        self.dw_dn = theano.shared(value=zero_ary, name='vcg_dw_dn')
        self.dw_gn = theano.shared(value=zero_ary, name='vcg_dw_gn')
        # init parameters for controlling learning dynamics
        self.set_dn_sgd_params() # init SGD rate/momentum for DN
        self.set_gn_sgd_params() # init SGD rate/momentum for GN
        self.set_in_sgd_params() # init SGD rate/momentum for IN
        
        self.set_disc_weights()  # init adversarial cost weights for GN/DN
        self.lam_l2d = theano.shared(value=(zero_ary + params['lam_l2d']), \
                name='vcg_lam_l2d')

        nll_weights = np.linspace(0.0, 5.0, num=self.chain_len)
        nll_weights = nll_weights / np.sum(nll_weights)
        nll_weights = nll_weights.astype(theano.config.floatX)
        self.mask_nll_weights = theano.shared(value=nll_weights, \
                name='vcg_mask_nll_weights')

        # Grab the full set of "optimizable" parameters from the generator
        # and discriminator networks that we'll be working with. We need to
        # ignore parameters in the final layers of the proto-networks in the
        # discriminator network (a generalized pseudo-ensemble). We ignore them
        # because the VCGair requires that they be "bypassed" in favor of some
        # binary classification layers that will be managed by this VCGair.
        self.dn_params = []
        for pn in self.DN.proto_nets:
            for pnl in pn[0:-1]:
                self.dn_params.extend(pnl.params)
        self.in_params = [p for p in self.IN.mlp_params]
        self.gn_params = [p for p in self.GN.mlp_params]

        # Now construct a binary discriminator layer for each proto-net in the
        # discriminator network. And, add their params to optimization list.
        self._construct_disc_layers(rng)
        self.disc_reg_cost = self.lam_l2d[0] * \
                T.sum([dl.act_l2_sum for dl in self.disc_layers])

        # Construct costs for the generator and discriminator networks based 
        # on adversarial binary classification
        self.disc_cost_dn, self.disc_cost_gn = self._construct_disc_costs()

        # first, build the cost to be optimized by the discriminator network,
        # in general this will be treated somewhat indepedently of the
        # optimization of the generator and inferencer networks.
        self.dn_cost = self.disc_cost_dn + self.DN.act_reg_cost + \
                self.disc_reg_cost
        # construct costs relevant to the optimization of the generator and
        # discriminator networks
        self.chain_nll_cost = self.lam_chain_nll[0] * \
                self._construct_chain_nll_cost(data_weight=0.9)
        self.chain_kld_cost = self.lam_chain_kld[0] * \
                self._construct_chain_kld_cost(data_weight=0.9)
        self.chain_vel_cost = self.lam_chain_vel[0] * \
                self._construct_chain_vel_cost()
        self.mask_nll_cost = self.lam_mask_nll[0] * \
                self._construct_mask_nll_cost()
        self.mask_kld_cost = self.lam_mask_kld[0] * \
                self._construct_mask_kld_cost()
        self.other_reg_cost = self._construct_other_reg_cost()
        self.gip_cost = self.disc_cost_gn + self.chain_nll_cost + \
                self.chain_kld_cost + self.chain_vel_cost + \
                self.mask_nll_cost + self.mask_kld_cost + \
                self.other_reg_cost
        # compute total cost on the discriminator and VB generator/inferencer
        self.joint_cost = self.dn_cost + self.gip_cost

        # Initialize momentums for mini-batch SGD updates. All parameters need
        # to be safely nestled in their lists by now.
        self.joint_moms = OrderedDict()
        self.dn_moms = OrderedDict()
        self.in_moms = OrderedDict()
        self.gn_moms = OrderedDict()
        for p in self.dn_params:
            p_mo = np.zeros(p.get_value(borrow=True).shape) + 5.0
            self.dn_moms[p] = theano.shared(value=p_mo.astype(theano.config.floatX))
            self.joint_moms[p] = self.dn_moms[p]
        for p in self.in_params:
            p_mo = np.zeros(p.get_value(borrow=True).shape) + 5.0
            self.in_moms[p] = theano.shared(value=p_mo.astype(theano.config.floatX))
            self.joint_moms[p] = self.in_moms[p]
        for p in self.gn_params:
            p_mo = np.zeros(p.get_value(borrow=True).shape) + 5.0
            self.gn_moms[p] = theano.shared(value=p_mo.astype(theano.config.floatX))
            self.joint_moms[p] = self.gn_moms[p]

        # Construct the updates for the generator and discriminator network
        self.joint_updates = OrderedDict()
        self.dn_updates = OrderedDict()
        self.in_updates = OrderedDict()
        self.gn_updates = OrderedDict()

        ###########################################
        # Construct updates for the discriminator #
        ###########################################
        for var in self.dn_params:
            # these updates are for trainable params in the inferencer net...
            # first, get gradient of cost w.r.t. var
            var_grad = T.grad(self.dn_cost, var, \
                    consider_constant=[self.GN.dist_mean, self.GN.dist_cov]).clip(-0.1,0.1)
            # get the momentum for this var
            var_mom = self.dn_moms[var]
            # update the momentum for this var using its grad
            self.dn_updates[var_mom] = (self.mo_dn[0] * var_mom) + \
                    ((1.0 - self.mo_dn[0]) * (var_grad**2.0))
            self.joint_updates[var_mom] = self.dn_updates[var_mom]
            # make basic update to the var
            var_new = var - (self.lr_dn[0] * (var_grad / T.sqrt(var_mom + 1e-3)))
            self.dn_updates[var] = var_new
            # add this var's update to the joint updates too
            self.joint_updates[var] = self.dn_updates[var]
        ########################################
        # Construct updates for the inferencer #
        ########################################
        for var in self.in_params:
            # these updates are for trainable params in the generator net...
            # first, get gradient of cost w.r.t. var
            var_grad = T.grad(self.gip_cost, var, \
                    consider_constant=[self.GN.dist_mean, self.GN.dist_cov]).clip(-0.1,0.1)
            # get the momentum for this var
            var_mom = self.in_moms[var]
            # update the momentum for this var using its grad
            self.in_updates[var_mom] = (self.mo_in[0] * var_mom) + \
                    ((1.0 - self.mo_in[0]) * (var_grad**2.0))
            self.joint_updates[var_mom] = self.in_updates[var_mom]
            # make basic update to the var
            var_new = var - (self.lr_in[0] * (var_grad / T.sqrt(var_mom + 1e-3)))
            self.in_updates[var] = var_new
            # add this var's update to the joint updates too
            self.joint_updates[var] = self.in_updates[var]
        #######################################
        # Construct updates for the generator #
        #######################################
        for var in self.gn_params:
            # these updates are for trainable params in the generator net...
            # first, get gradient of cost w.r.t. var
            var_grad = T.grad(self.gip_cost, var, \
                    consider_constant=[self.GN.dist_mean, self.GN.dist_cov]).clip(-0.1,0.1)
            # get the momentum for this var
            var_mom = self.gn_moms[var]
            # update the momentum for this var using its grad
            self.gn_updates[var_mom] = (self.mo_gn[0] * var_mom) + \
                    ((1.0 - self.mo_gn[0]) * (var_grad**2.0))
            self.joint_updates[var_mom] = self.gn_updates[var_mom]
            # make basic update to the var
            var_new = var - (self.lr_gn[0] * (var_grad / T.sqrt(var_mom + 1e-3)))
            self.gn_updates[var] = var_new
            # add this var's update to the joint updates too
            self.joint_updates[var] = self.gn_updates[var]

        # Construct the function for training on training data
        self.train_joint = self._construct_train_joint()

        # Construct a function for computing the ouputs of the generator
        # network for a batch of noise. Presumably, the noise will be drawn
        # from the same distribution that was used in training....
        self.sample_chain_from_data = self.GIP.sample_gil_from_data
        return

    def set_dn_sgd_params(self, learn_rate=0.02, momentum=0.9):
        """
        Set learning rate and momentum parameter for discriminator updates.
        """
        zero_ary = np.zeros((1,))
        new_lr = zero_ary + learn_rate
        self.lr_dn.set_value(new_lr.astype(theano.config.floatX))
        new_mo = zero_ary + momentum
        self.mo_dn.set_value(new_mo.astype(theano.config.floatX))
        return

    def set_in_sgd_params(self, learn_rate=0.02, momentum=0.9):
        """
        Set learning rate and momentum parameter for self.PN updates.
        """
        zero_ary = np.zeros((1,))
        new_lr = zero_ary + learn_rate
        self.lr_in.set_value(new_lr.astype(theano.config.floatX))
        new_mo = zero_ary + momentum
        self.mo_in.set_value(new_mo.astype(theano.config.floatX))
        return

    def set_gn_sgd_params(self, learn_rate=0.02, momentum=0.9):
        """
        Set learning rate and momentum parameter for generator updates.
        """
        zero_ary = np.zeros((1,))
        new_lr = zero_ary + learn_rate
        self.lr_gn.set_value(new_lr.astype(theano.config.floatX))
        new_mo = zero_ary + momentum
        self.mo_gn.set_value(new_mo.astype(theano.config.floatX))
        return

    def set_all_sgd_params(self, learn_rate=0.02, momentum=0.9):
        """
        Set learning rate and momentum parameter for all updates.
        """
        zero_ary = np.zeros((1,))
        # set learning rates
        new_lr = zero_ary + learn_rate
        self.lr_dn.set_value(new_lr.astype(theano.config.floatX))
        self.lr_gn.set_value(new_lr.astype(theano.config.floatX))
        self.lr_in.set_value(new_lr.astype(theano.config.floatX))
        # set momentums
        new_mo = zero_ary + momentum
        self.mo_dn.set_value(new_mo.astype(theano.config.floatX))
        self.mo_gn.set_value(new_mo.astype(theano.config.floatX))
        self.mo_in.set_value(new_mo.astype(theano.config.floatX))
        return

    def set_disc_weights(self, dweight_gn=1.0, dweight_dn=1.0):
        """
        Set weights for the adversarial classification cost.
        """
        zero_ary = np.zeros((1,)).astype(theano.config.floatX)
        new_dw_dn = zero_ary + dweight_dn
        self.dw_dn.set_value(new_dw_dn)
        new_dw_gn = zero_ary + dweight_gn
        self.dw_gn.set_value(new_dw_gn)
        return

    def set_lam_chain_nll(self, lam_chain_nll=1.0):
        """
        Set weight for controlling the influence of the data likelihood.
        """
        zero_ary = np.zeros((1,))
        new_lam = zero_ary + lam_chain_nll
        self.lam_chain_nll.set_value(new_lam.astype(theano.config.floatX))
        return

    def set_lam_chain_kld(self, lam_chain_kld=1.0):
        """
        Set the strength of regularization on KL-divergence for continuous
        posterior variables. When set to 1.0, this reproduces the standard
        role of KL(posterior || prior) in variational learning.
        """
        zero_ary = np.zeros((1,))
        new_lam = zero_ary + lam_chain_kld
        self.lam_chain_kld.set_value(new_lam.astype(theano.config.floatX))
        return

    def set_lam_chain_vel(self, lam_chain_vel=1.0):
        """
        Set the strength of regularization on Markov Chain velocity.
        """
        zero_ary = np.zeros((1,))
        new_lam = zero_ary + lam_chain_vel
        self.lam_chain_vel.set_value(new_lam.astype(theano.config.floatX))
        return

    def set_lam_mask_nll(self, lam_mask_nll=0.0):
        """
        Set weight for controlling the influence of the data likelihood.
        """
        zero_ary = np.zeros((1,))
        new_lam = zero_ary + lam_mask_nll
        self.lam_mask_nll.set_value(new_lam.astype(theano.config.floatX))
        return

    def set_lam_mask_kld(self, lam_mask_kld=1.0):
        """
        Set the strength of regularization on KL-divergence for continuous
        posterior variables. When set to 1.0, this reproduces the standard
        role of KL(posterior || prior) in variational learning.
        """
        zero_ary = np.zeros((1,))
        new_lam = zero_ary + lam_mask_kld
        self.lam_mask_kld.set_value(new_lam.astype(theano.config.floatX))
        return

    def set_lam_l2w(self, lam_l2w=1e-3):
        """
        Set the relative strength of l2 regularization on network params.
        """
        zero_ary = np.zeros((1,))
        new_lam = zero_ary + lam_l2w
        self.lam_l2w.set_value(new_lam.astype(theano.config.floatX))
        return

    def _construct_disc_layers(self, rng):
        """
        Construct binary discrimination layers for each spawn-net in the
        underlying discrimnator pseudo-ensemble. All spawn-nets spawned from
        the same proto-net will use the same disc-layer parameters.
        """
        self.disc_layers = []
        self.disc_outputs = []
        for sn in self.DN.spawn_nets:
            # construct a "binary discriminator" layer to sit on top of each
            # spawn net in the discriminator pseudo-ensemble
            sn_fl = sn[-1]
            self.disc_layers.append(DiscLayer(rng=rng, \
                    input=sn_fl.noisy_input, in_dim=sn_fl.in_dim))
            # capture the (linear) output of the DiscLayer, for possible reuse
            self.disc_outputs.append(self.disc_layers[-1].linear_output)
            # get the params of this DiscLayer, for convenient optimization
            self.dn_params.extend(self.disc_layers[-1].params)
        return

    def _construct_disc_costs(self):
        """
        Construct the generator and discriminator adversarial costs.
        """
        gn_costs = []
        dn_costs = []
        for dl_output in self.disc_outputs:
            data_preds = dl_output.take(self.It, axis=0)
            noise_preds = dl_output.take(self.Id, axis=0)
            # compute the cost with respect to which we will be optimizing
            # the parameters of the discriminator network
            data_size = T.cast(self.It.size, 'floatX')
            noise_size = T.cast(self.Id.size, 'floatX')
            k_ns_nce = noise_size / data_size
            #dnl_dn_cost = (ns_nce_pos(data_preds, k=k_ns_nce) + \
            #               ns_nce_neg(noise_preds, k=k_ns_nce)) / \
            #               (data_size + noise_size)
            dnl_dn_cost = (logreg_loss(data_preds, 1.0) / data_size) + \
                          (logreg_loss(noise_preds, -1.0) / noise_size)
            # compute the cost with respect to which we will be optimizing
            # the parameters of the generative model
            dnl_gn_cost = ulh_loss(noise_preds, 0.0) / noise_size
            dn_costs.append(dnl_dn_cost)
            gn_costs.append(dnl_gn_cost)
        dn_cost = self.dw_dn[0] * T.sum(dn_costs)
        gn_cost = self.dw_gn[0] * T.sum(gn_costs)
        return [dn_cost, gn_cost]

    def _construct_chain_nll_cost(self, data_weight=0.5):
        """
        Construct the negative log-likelihood part of cost to minimize.

        This is for operation in "free chain" mode, where a seed point is used
        to initialize a long(ish) running markov chain.
        """
        assert((data_weight > 0.0) and (data_weight < 1.0))
        obs_count = T.cast(self.Xd.shape[0], 'floatX')
        nll_costs = []
        cost_0 = data_weight
        cost_1 = (1.0 - data_weight) * (1.0 / (self.chain_len - 1))
        for i in range(self.chain_len):
            IN_i = self.IN_chain[i]
            GN_i = self.GN_chain[i]
            c = -T.sum(GN_i.compute_log_prob(Xd=IN_i.Xd)) / obs_count
            if (i == 0):
                nll_costs.append(cost_0 * c)
            else:
                nll_costs.append(cost_1 * c)
        nll_cost = sum(nll_costs)
        return nll_cost

    def _construct_chain_kld_cost(self, data_weight=0.5):
        """
        Construct the posterior KL-d from prior part of cost to minimize.

        This is for operation in "free chain" mode, where a seed point is used
        to initialize a long(ish) running markov chain.
        """
        assert((data_weight > 0.0) and (data_weight < 1.0))
        obs_count = T.cast(self.Xd.shape[0], 'floatX')
        kld_costs = []
        cost_0 = data_weight
        cost_1 = (1.0 - data_weight) * (1.0 / (self.chain_len - 1))
        for i in range(self.chain_len):
            IN_i = self.IN_chain[i]
            c = T.sum(IN_i.kld_cost) / obs_count
            if (i == 0):
                kld_costs.append(cost_0 * c)
            else:
                kld_costs.append(cost_1 * c)
        kld_cost = sum(kld_costs)
        return kld_cost

    def _construct_chain_vel_cost(self):
        """
        Construct the Markov Chain velocity part of cost to minimize.

        This is for operation in "free chain" mode, where a seed point is used
        to initialize a long(ish) running markov chain.
        """
        obs_count = T.cast(self.Xd.shape[0], 'floatX')
        IN_start = self.IN_chain[0]
        GN_end = self.GN_chain[-1]
        vel_cost = T.sum(GN_end.compute_log_prob(Xd=IN_start.Xd)) / obs_count
        return vel_cost

    def _construct_mask_nll_cost(self):
        """
        Construct the negative log-likelihood part of cost to minimize.

        This is for "iterative reconstruction" when the seed input is subject
        to partial masking.
        """
        obs_count = T.cast(self.Xd.shape[0], 'floatX')
        nll_costs = []
        for i in range(self.chain_len):
            IN_i = self.IN_chain[i]
            GN_i = self.GN_chain[i]
            c = -T.sum(GN_i.masked_log_prob(Xc=self.Xc, Xm=self.Xm)) \
                    / obs_count
            nll_costs.append(self.mask_nll_weights[i] * c)
        nll_cost = sum(nll_costs)
        return nll_cost

    def _construct_mask_kld_cost(self):
        """
        Construct the posterior KL-d from prior part of cost to minimize.

        This is for "iterative reconstruction" when the seed input is subject
        to partial masking.
        """
        obs_count = T.cast(self.Xd.shape[0], 'floatX')
        kld_costs = []
        for i in range(self.chain_len):
            IN_i = self.IN_chain[i]
            c = T.sum(IN_i.kld_cost) / obs_count
            kld_costs.append(c)
        kld_cost = sum(kld_costs) / float(self.chain_len)
        return kld_cost

    def _construct_other_reg_cost(self):
        """
        Construct the cost for low-level basic regularization. E.g. for
        applying l2 regularization to the network activations and parameters.
        """
        gp_cost = sum([T.sum(par**2.0) for par in self.gn_params])
        ip_cost = sum([T.sum(par**2.0) for par in self.in_params])
        other_reg_cost = self.lam_l2w[0] * (gp_cost + ip_cost)
        return other_reg_cost

    def _construct_train_joint(self):
        """
        Construct theano function to train generator and discriminator jointly.
        """
        outputs = [self.joint_cost, self.chain_nll_cost, self.chain_kld_cost, \
                self.chain_vel_cost, self.mask_nll_cost, self.mask_kld_cost, \
                self.disc_cost_gn, self.disc_cost_dn, self.other_reg_cost]
        func = theano.function(inputs=[ self.Xd, self.Xc, self.Xm, self.Xt ], \
                outputs=outputs, updates=self.joint_updates) # , \
                #mode=profmode)
        #theano.printing.pydotprint(func, \
        #    outfile='VCG_train_joint.png', compact=True, format='png', with_ids=False, \
        #    high_contrast=True, cond_highlight=None, colorCodes=None, \
        #    max_label_size=70, scan_graphs=False, var_with_name_simple=False, \
        #    print_output_file=True, assert_nb_all_strings=-1)
        return func

    def sample_from_prior(self, samp_count):
        """
        Draw independent samples from the model's prior, using the gaussian
        continuous prior of the underlying GenNet.
        """
        Zs = self.GN.sample_from_prior(samp_count).astype(theano.config.floatX)
        Xs = self.GN.transform_prior(Zs)
        return Xs

if __name__=="__main__":
    import time
    import utils as utils
    from load_data import load_udm, load_udm_ss, load_mnist
    from PeaNet import PeaNet
    from InfNet import InfNet
    from GenNet import GenNet
    from GIPair import GIPair
    from NetLayers import relu_actfun, softplus_actfun, \
                          safe_softmax, safe_log

    import sys, resource
    resource.setrlimit(resource.RLIMIT_STACK, (2**29,-1))
    sys.setrecursionlimit(10**6)

    # Simple test code, to check that everything is basically functional.
    print("TESTING...")

    # Initialize a source of randomness
    rng = np.random.RandomState(1234)

    # Load some data to train/validate/test with
    dataset = 'data/mnist.pkl.gz'
    datasets = load_udm(dataset, zero_mean=False)
    Xtr = datasets[0][0]
    Xtr = Xtr.get_value(borrow=False)
    Xva = datasets[1][0]
    Xva = Xva.get_value(borrow=False)
    print("Xtr.shape: {0:s}, Xva.shape: {1:s}".format(str(Xtr.shape),str(Xva.shape)))

    # get and set some basic dataset information
    tr_samples = Xtr.shape[0]
    data_dim = Xtr.shape[1]
    batch_size = 128
    prior_dim = 75
    prior_sigma = 1.0
    Xtr_mean = np.mean(Xtr, axis=0, keepdims=True)
    Xtr_mean = (0.0 * Xtr_mean) + np.mean(Xtr)
    Xc_mean = np.repeat(Xtr_mean, batch_size, axis=0).astype(theano.config.floatX)

    # Symbolic inputs
    Xd = T.matrix(name='Xd')
    Xc = T.matrix(name='Xc')
    Xm = T.matrix(name='Xm')
    Xt = T.matrix(name='Xt')
    Xp = T.matrix(name='Xp')

    ###############################
    # Setup discriminator network #
    ###############################
    # Set some reasonable mlp parameters
    dn_params = {}
    # Set up some proto-networks
    pc0 = [data_dim, (250, 4), (250, 4), 10]
    dn_params['proto_configs'] = [pc0]
    # Set up some spawn networks
    sc0 = {'proto_key': 0, 'input_noise': 0.1, 'bias_noise': 0.1, 'do_dropout': True}
    #sc1 = {'proto_key': 0, 'input_noise': 0.1, 'bias_noise': 0.1, 'do_dropout': True}
    dn_params['spawn_configs'] = [sc0]
    dn_params['spawn_weights'] = [1.0]
    # Set remaining params
    dn_params['ear_type'] = 2
    dn_params['ear_lam'] = 0.0
    dn_params['lam_l2a'] = 1e-2
    dn_params['vis_drop'] = 0.2
    dn_params['hid_drop'] = 0.5
    dn_params['init_scale'] = 2.0
    # Initialize a network object to use as the discriminator
    DN = PeaNet(rng=rng, Xd=Xd, params=dn_params)
    DN.init_biases(0.0)

    ############################
    # Setup inferencer network #
    ############################
    # choose some parameters for the continuous inferencer
    in_params = {}
    shared_config = [data_dim, (250, 4), (250, 4)]
    top_config = [shared_config[-1], (125, 4), prior_dim]
    in_params['shared_config'] = shared_config
    in_params['mu_config'] = top_config
    in_params['sigma_config'] = top_config
    in_params['activation'] = softplus_actfun
    in_params['init_scale'] = 2.0
    in_params['lam_l2a'] = 1e-2
    in_params['vis_drop'] = 0.0
    in_params['hid_drop'] = 0.0
    in_params['bias_noise'] = 0.1
    in_params['input_noise'] = 0.0
    IN = InfNet(rng=rng, Xd=Xd, Xc=Xc, Xm=Xm, \
            prior_sigma=prior_sigma, params=in_params)
    IN.init_biases(0.0)

    ###########################
    # Setup generator network #
    ###########################
    # Choose some parameters for the generative network
    gn_params = {}
    gn_config = [prior_dim, 1000, 1000, data_dim]
    gn_params['mlp_config'] = gn_config
    gn_params['lam_l2a'] = 1e-2
    gn_params['vis_drop'] = 0.0
    gn_params['hid_drop'] = 0.0
    gn_params['bias_noise'] = 0.1
    gn_params['out_type'] = 'bernoulli'
    gn_params['activation'] = relu_actfun
    gn_params['init_scale'] = 2.0
    # Initialize a generator network object
    GN = GenNet(rng=rng, Xp=Xp, prior_sigma=prior_sigma, params=gn_params)
    GN.init_biases(0.1)

    ################################
    # Initialize the main VCGChain #
    ################################
    vcg_params = {}
    vcg_params['lam_l2d'] = 1e-2
    VCG = VCGChain(rng=rng, Xd=Xd, Xc=Xc, Xm=Xm, Xt=Xt, i_net=IN, \
                 g_net=GN, d_net=DN, chain_len=8, data_dim=data_dim, \
                 prior_dim=prior_dim, params=vcg_params)
    VCG.set_lam_l2w(1e-4)

    ###################################
    # TRAIN AS MARKOV CHAIN WITH BPTT #
    ###################################
    learn_rate = 0.0002
    for i in range(1000000):
        scale = float(min((i+1), 25000)) / 25000.0
        if ((i+1 % 100000) == 0):
            learn_rate = learn_rate * 0.75
        if True:
            ########################################
            # TRAIN THE CHAIN IN FREE-RUNNING MODE #
            ########################################
            VCG.set_all_sgd_params(learn_rate=(scale*learn_rate), momentum=0.98)
            VCG.set_dn_sgd_params(learn_rate=(0.4*scale*learn_rate), momentum=0.98)
            VCG.set_disc_weights(dweight_gn=5.0, dweight_dn=5.0)
            VCG.set_lam_chain_nll(1.0)
            VCG.set_lam_chain_kld(0.05 + (1.0*scale))
            VCG.set_lam_chain_vel(0.0)
            VCG.set_lam_mask_nll(0.0)
            VCG.set_lam_mask_kld(0.0)
            # get some data to train with
            tr_idx = npr.randint(low=0,high=tr_samples,size=(batch_size,))
            Xd_batch = Xtr.take(tr_idx, axis=0)
            Xc_batch = 0.0 * Xd_batch
            Xm_batch = 0.0 * Xd_batch
            tr_idx = npr.randint(low=0,high=tr_samples,size=(batch_size,))
            Xt_batch = Xtr.take(tr_idx, axis=0)
            # do a minibatch update of the model, and compute some costs
            outputs = VCG.train_joint(Xd_batch, Xc_batch, Xm_batch, Xt_batch)
            joint_cost_1 = 1.0 * outputs[0]
            chain_nll_cost_1= 1.0 * outputs[1]
            chain_kld_cost_1 = 1.0 * outputs[2]
            chain_vel_cost_1 = 1.0 * outputs[3]
            mask_nll_cost_1 = 1.0 * outputs[4]
            mask_kld_cost_1 = 1.0 * outputs[5]
            disc_cost_gn_1 = 1.0 * outputs[6]
            disc_cost_dn_1 = 1.0 * outputs[7]
            other_reg_cost_1 = 1.0 * outputs[8]
        if ((i % 4) == 0):
            #########################################
            # TRAIN THE CHAIN UNDER PARTIAL CONTROL #
            #########################################
            VCG.set_all_sgd_params(learn_rate=(scale*learn_rate), momentum=0.98)
            VCG.set_dn_sgd_params(learn_rate=(0.4*scale*learn_rate), momentum=0.98)
            VCG.set_disc_weights(dweight_gn=5.0, dweight_dn=5.0)
            VCG.set_lam_chain_nll(0.0)
            VCG.set_lam_chain_kld(0.0)
            VCG.set_lam_chain_vel(0.0)
            VCG.set_lam_mask_nll(1.0)
            VCG.set_lam_mask_kld(0.05 + (0.95*scale))
            # get some data to train with
            tr_idx = npr.randint(low=0,high=tr_samples,size=(batch_size,))
            Xd_batch = Xc_mean
            Xc_batch = Xtr.take(tr_idx, axis=0)
            Xm_rand = sample_masks(Xc_batch, drop_prob=0.3)
            Xm_patch = sample_patch_masks(Xc_batch, (28,28), (14,14))
            Xm_batch = Xm_rand * Xm_patch
            tr_idx = npr.randint(low=0,high=tr_samples,size=(batch_size,))
            Xt_batch = Xtr.take(tr_idx, axis=0)
            # do a minibatch update of the model, and compute some costs
            outputs = VCG.train_joint(Xd_batch, Xc_batch, Xm_batch, Xt_batch)
            joint_cost_2 = 1.0 * outputs[0]
            chain_nll_cost_2 = 1.0 * outputs[1]
            chain_kld_cost_2 = 1.0 * outputs[2]
            chain_vel_cost_2 = 1.0 * outputs[3]
            mask_nll_cost_2 = 1.0 * outputs[4]
            mask_kld_cost_2 = 1.0 * outputs[5]
            disc_cost_gn_2 = 1.0 * outputs[6]
            disc_cost_dn_2 = 1.0 * outputs[7]
            other_reg_cost_2 = 1.0 * outputs[8]
        if ((i % 1000) == 0):
            print("batch: {0:d}, joint_cost: {1:.4f}, chain_nll_cost: {2:.4f}, chain_kld_cost: {3:.4f}, chain_vel_cost: {4:.4f}, disc_cost_gn: {5:.4f}, disc_cost_dn: {6:.4f}".format( \
                    i, joint_cost_1, chain_nll_cost_1, chain_kld_cost_1, chain_vel_cost_1, disc_cost_gn_1, disc_cost_dn_1))
            print("------ {0:d}, joint_cost: {1:.4f}, mask_nll_cost: {2:.4f}, mask_kld_cost: {3:.4f}, disc_cost_gn: {4:.4f}, disc_cost_dn: {5:.4f}".format( \
                    i, joint_cost_2, mask_nll_cost_2, mask_kld_cost_2, disc_cost_gn_2, disc_cost_dn_2))
        if ((i % 5000) == 0):
            tr_idx = npr.randint(low=0,high=Xtr.shape[0],size=(5,))
            va_idx = npr.randint(low=0,high=Xva.shape[0],size=(5,))
            Xd_batch = np.vstack([Xtr.take(tr_idx, axis=0), Xva.take(va_idx, axis=0)])
            # draw some chains of samples from the VAE loop
            file_name = "VCG_AAA_CHAIN_SAMPLES_b{0:d}.png".format(i)
            Xd_samps = np.repeat(Xd_batch, 3, axis=0)
            sample_lists = VCG.GIP.sample_gil_from_data(Xd_samps, loop_iters=20)
            Xs = np.vstack(sample_lists["data samples"])
            utils.visualize_samples(Xs, file_name, num_rows=20)
            # draw some masked chains of samples from the VAE loop
            file_name = "VCG_AAA_MASK_SAMPLES_b{0:d}.png".format(i)
            Xd_samps = np.repeat(Xc_mean[0:Xd_batch.shape[0],:], 3, axis=0)
            Xc_samps = np.repeat(Xd_batch, 3, axis=0)
            Xm_rand = sample_masks(Xc_samps, drop_prob=0.3)
            Xm_patch = sample_patch_masks(Xc_samps, (28,28), (14,14))
            Xm_samps = Xm_rand * Xm_patch
            sample_lists = VCG.GIP.sample_gil_from_data(Xd_samps, \
                    X_c=Xc_samps, X_m=Xm_samps, loop_iters=20)
            Xs = np.vstack(sample_lists["data samples"])
            utils.visualize_samples(Xs, file_name, num_rows=20)
            # draw some samples independently from the GenNet's prior
            file_name = "VCG_AAA_PRIOR_SAMPLES_b{0:d}.png".format(i)
            Xs = VCG.sample_from_prior(20*20)
            utils.visualize_samples(Xs, file_name, num_rows=20)
            # draw discriminator network's weights
            file_name = "VCG_AAA_DIS_WEIGHTS_b{0:d}.png".format(i)
            utils.visualize_net_layer(VCG.DN.proto_nets[0][0], file_name)
            # draw inference net first layer weights
            file_name = "VCG_AAA_INF_WEIGHTS_b{0:d}.png".format(i)
            utils.visualize_net_layer(VCG.IN.shared_layers[0], file_name)
            # draw generator net final layer weights
            file_name = "VCG_AAA_GEN_WEIGHTS_b{0:d}.png".format(i)
            utils.visualize_net_layer(VCG.GN.mlp_layers[-1], file_name, use_transpose=True)
    print("TESTING COMPLETE!")
    profmode.print_summary()




##############
# EYE BUFFER #
##############
