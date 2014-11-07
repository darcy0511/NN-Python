###################################################################
# Code for managing and training a generator/discriminator pair.  #
###################################################################

# basic python
import numpy as np
import numpy.random as npr
from collections import OrderedDict

# theano business
import theano
import theano.tensor as T
from theano.ifelse import ifelse
import theano.tensor.shared_randomstreams
#from theano.sandbox.cuda.rng_curand import CURAND_RandomStreams

# phil's sweetness
from NetLayers import HiddenLayer, DiscLayer
from GenNet import projected_moments

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
    loss = T.sum(T.log(1.0 + T.exp(-class_sign * Y)))
    return loss

def lsq_loss(Yh, Yt=0.0):
    """
    Least-squares loss for predictions in Yh, given target Yt.
    """
    loss = T.sum((Yh - Yt)**2.0)
    return loss

def ulh_loss(Yh, Yt=0.0, delta=0.5):
    """
    Unilateral Huberized least-squares loss for Yh, given target Yt.
    """
    quad_loss = (Yh - Yt)**2.0
    line_loss = (2.0 * delta * abs(Yh - Yt)) - delta**2.0
    # Construct masks for cost-type regions
    neg_mask = Yh < 0.0
    quad_mask = (abs(Yh) < delta) * neg_mask
    line_mask = (abs(Yh) >= delta) * neg_mask
    # Construct masked loss
    loss = T.sum((quad_loss * quad_mask) + (line_loss * line_mask))
    return loss

class GCPair(object):
    """
    Controller for training a generator/discriminator pair.

    The generator must be an instance of the GEN_NET class implemented in
    "GINets.py". The discriminator must be an instance of the EarNet class,
    as implemented in "EarNet.py".

    Parameters:
        rng: numpy.random.RandomState (for reproducibility)
        d_net: The EarNet instance that will serve as the discriminator
        g_net: The GenNet instance that will serve as the generator
        data_dim: Dimensions of generated data
        params: a dict of parameters for controlling various costs
            lam_l2d: regularization on squared discriminator output
            mom_mix_rate: rate for updates to the running moment estimates
                          for the distribution generated by g_net
            mom_match_weight: weight for the "moment matching" cost
            target_mean: first-order moment to try and match with g_net
            target_cov: second-order moment to try and match with g_net
    """
    def __init__(self, rng=None, d_net=None, g_net=None, data_dim=None, \
            data_var=None, params=None):
        # Do some stuff!
        self.rng = theano.tensor.shared_randomstreams.RandomStreams( \
                rng.randint(100000))
        self.DN = d_net
        self.GN = g_net
        self.input_noise = self.GN.input_var
        self.input_data = data_var
        self.sample_data = self.GN.output
        self.data_dim = data_dim
        # set input to the discriminator to be the true data samples
        # concatenated with the samples from the generator network
        #self.DN.input = T.vertical_stack(self.input_data, self.sample_data)

        # symbolic var data input
        self.Xd = T.matrix(name='gcp_Xd')
        # symbolic var noise input
        self.Xn = T.matrix(name='gcp_Xn')
        # symbolic matrix of indices for data inputs
        self.Id = T.lvector(name='gcp_Id')
        # symbolic matrix of indices for noise inputs
        self.In = T.lvector(name='gcp_In')
        # shared var learning rate for generator and discriminator
        zero_ary = np.zeros((1,)).astype(theano.config.floatX)
        self.lr_gn = theano.shared(value=zero_ary, name='gcp_lr_gn')
        self.lr_dn = theano.shared(value=zero_ary, name='gcp_lr_dn')
        # shared var momentum parameters for generator and discriminator
        self.mo_gn = theano.shared(value=zero_ary, name='gcp_mo_gn')
        self.mo_dn = theano.shared(value=zero_ary, name='gcp_mo_dn')
        # shared var weights for adversarial classification objective
        self.dw_gn = theano.shared(value=zero_ary, name='gcp_dw_gn')
        self.dw_dn = theano.shared(value=zero_ary, name='gcp_dw_dn')
        # init parameters for controlling learning dynamics
        self.set_gn_sgd_params() # init SGD rate/momentum for GN
        self.set_dn_sgd_params() # init SGD rate/momentum for DN
        self.set_disc_weights()  # init adversarial cost weights for GN/DN
        self.lam_l2d = theano.shared(value=(zero_ary + params['lam_l2d']), \
                name='gcp_lam_l2d')

        #######################################################
        # Welcome to: Moment Matching Cost Information Center #
        #######################################################
        #
        # Get parameters for managing the moment matching cost. The moment
        # matching is based on exponentially-decaying estimates of the mean
        # and covariance of the distribution induced by the generator network
        # and the (latent) noise being fed to it.
        #
        # We provide the option of performing moment matching with either the
        # raw generator output, or with linearly-transformed generator output.
        # Either way, the given target mean and covariance should have the
        # appropriate dimension for the space in which we'll be matching the
        # generator's 1st/2nd moments with the target's 1st/2nd moments. For
        # clarity, the computation we'll perform looks like:
        #
        #   Xm = X - np.mean(X, axis=0)
        #   XmP = np.dot(Xm, P)
        #   C = np.dot(XmP.T, XmP)
        #
        # where Xm is the mean-centered samples from the generator and P is
        # the matrix for the linear transform to apply prior to computing
        # the moment matching cost. For simplicity, the above code ignores the
        # use of an exponentially decaying average to track the estimated mean
        # and covariance of the generator's output distribution.
        #
        # The relative contribution of the current batch to these running
        # estimates is determined by self.mom_mix_rate. The mean estimate is
        # first updated based on the current batch, then the current batch
        # is centered with the updated mean, then the covariance estimate is
        # updated with the mean-centered samples in the current batch.
        #
        # Strength of the moment matching cost is given by self.mom_match_cost.
        # Target mean/covariance are given by self.target_mean/self.target_cov.
        # If a linear transform is to be applied prior to matching, it is given
        # by self.mom_match_proj.
        #
        zero_ary = np.zeros((1,))
        mmr = zero_ary + params['mom_mix_rate']
        self.mom_mix_rate = theano.shared(name='gcp_mom_mix_rate', \
            value=mmr.astype(theano.config.floatX))
        mmw = zero_ary + params['mom_match_weight']
        self.mom_match_weight = theano.shared(name='gcp_mom_match_weight', \
            value=mmw.astype(theano.config.floatX))
        targ_mean = params['target_mean'].astype(theano.config.floatX)
        targ_cov = params['target_cov'].astype(theano.config.floatX)
        assert(targ_mean.size == targ_cov.shape[0]) # mean and cov use same dim
        assert(targ_cov.shape[0] == targ_cov.shape[1]) # cov must be square
        self.target_mean = theano.shared(value=targ_mean, name='gcp_target_mean')
        self.target_cov = theano.shared(value=targ_cov, name='gcp_target_cov')
        mmp = np.identity(targ_cov.shape[0]) # default to identity transform
        if 'mom_match_proj' in params:
            mmp = params['mom_match_proj'] # use a user-specified transform
        assert(mmp.shape[0] == self.data_dim) # transform matches data dim
        assert(mmp.shape[1] == targ_cov.shape[0]) # and matches mean/cov dims
        mmp = mmp.astype(theano.config.floatX)
        self.mom_match_proj = theano.shared(value=mmp, name='gcp_mom_map_proj')
        # finally, we can construct the moment matching cost! and the updates
        # for the running mean/covariance estimates too!
        self.mom_match_cost, self.mom_updates = self._construct_mom_stuff()
        #########################################
        # Thank you for visiting the M.M.C.I.C. #
        #########################################

        # Grab the full set of "optimizable" parameters from the generator
        # and discriminator networks that we'll be working with. We need to
        # ignore parameters in the final layers of the proto-networks in the
        # discriminator network (a generalized pseudo-ensemble). We ignore them
        # because the GCPair requires that they be "bypassed" in favor of some
        # binary classification layers that will be managed by this GCPair.
        self.dn_params = []
        for pn in self.DN.proto_nets:
            for pnl in pn[0:-1]:
                self.dn_params.extend(pnl.params)
        self.gn_params = [p for p in self.GN.mlp_params]
        # Now construct a binary discriminator layer for each proto-net in the
        # discriminator network. And, add their params to optimization list.
        self._construct_disc_layers(rng)
        self.disc_reg_cost = self.lam_l2d[0] * \
                T.sum([dl.act_l2_sum for dl in self.disc_layers])

        # Construct costs for the generator and discriminator networks based 
        # on adversarial binary classification
        self.disc_cost_dn, self.disc_cost_gn = self._construct_disc_costs()

        # Cost w.r.t. discriminator parameters is only the adversarial binary
        # classification cost. Cost w.r.t. comprises an adversarial binary
        # classification cost and the (weighted) moment matching cost.
        self.dn_cost = self.disc_cost_dn + self.DN.act_reg_cost + self.disc_reg_cost
        self.gn_cost = self.disc_cost_gn + self.mom_match_cost + self.GN.act_reg_cost

        # Initialize momentums for mini-batch SGD updates. All parameters need
        # to be safely nestled in their lists by now.
        self.joint_moms = OrderedDict()
        self.dn_moms = OrderedDict()
        self.gn_moms = OrderedDict()
        for p in self.dn_params:
            p_mo = np.zeros(p.get_value(borrow=True).shape)
            self.dn_moms[p] = theano.shared(value=p_mo.astype(theano.config.floatX))
            self.joint_moms[p] = self.dn_moms[p]
        for p in self.gn_params:
            p_mo = np.zeros(p.get_value(borrow=True).shape)
            self.gn_moms[p] = theano.shared(value=p_mo.astype(theano.config.floatX))
            self.joint_moms[p] = self.gn_moms[p]

        # Construct the updates for the generator and discriminator network
        self.joint_updates = OrderedDict()
        self.dn_updates = OrderedDict()
        self.gn_updates = OrderedDict()
        for var in self.dn_params:
            # these updates are for trainable params in the discriminator net...
            # first, get gradient of cost w.r.t. var
            var_grad = T.grad(self.dn_cost, var)
            # get the momentum for this var
            var_mom = self.dn_moms[var]
            # update the momentum for this var using its grad
            self.dn_updates[var_mom] = (self.mo_dn[0] * var_mom) + \
                    ((1.0 - self.mo_dn[0]) * var_grad)
            self.joint_updates[var_mom] = self.dn_updates[var_mom]
            # make basic update to the var
            var_new = var - (self.lr_dn[0] * var_mom)
            if ((var in self.DN.clip_params) and \
                    (var in self.DN.clip_norms) and \
                    (self.DN.clip_params[var] == 1)):
                # clip the basic updated var if it is set as clippable
                clip_norm = self.DN.clip_norms[var]
                var_norms = T.sum(var_new**2.0, axis=1, keepdims=True)
                var_scale = T.clip(T.sqrt(clip_norm / var_norms), 0., 1.)
                self.dn_updates[var] = var_new * var_scale
            else:
                # otherwise, just use the basic updated var
                self.dn_updates[var] = var_new
            # add this var's update to the joint updates too
            self.joint_updates[var] = self.dn_updates[var]
        for var in self.mom_updates:
            # these updates are for the generator distribution's running first
            # and second-order moment estimates
            self.gn_updates[var] = self.mom_updates[var]
            self.joint_updates[var] = self.gn_updates[var]
        for var in self.gn_params:
            # these updates are for trainable params in the generator net...
            # first, get gradient of cost w.r.t. var
            var_grad = T.grad(self.gn_cost, var, \
                    consider_constant=[self.GN.dist_mean, self.GN.dist_cov])
            # get the momentum for this var
            var_mom = self.gn_moms[var]
            # update the momentum for this var using its grad
            self.gn_updates[var_mom] = (self.mo_gn[0] * var_mom) + \
                    ((1.0 - self.mo_gn[0]) * var_grad)
            self.joint_updates[var_mom] = self.gn_updates[var_mom]
            # make basic update to the var
            var_new = var - (self.lr_gn[0] * var_mom)
            if ((var in self.GN.clip_params) and \
                    (var in self.GN.clip_norms) and \
                    (self.GN.clip_params[var] == 1)):
                # clip the basic updated var if it is set as clippable
                clip_norm = self.GN.clip_norms[var]
                var_norms = T.sum(var_new**2.0, axis=1, keepdims=True)
                var_scale = T.clip(T.sqrt(clip_norm / var_norms), 0., 1.)
                self.gn_updates[var] = var_new * var_scale
            else:
                # otherwise, just use the basic updated var
                self.gn_updates[var] = var_new
            # add this var's update to the joint updates too
            self.joint_updates[var] = self.gn_updates[var]

        # Construct batch-based training functions for the generator and
        # discriminator networks, as well as a joint training function.
        self.train_gn = self._construct_train_gn()
        self.train_dn = self._construct_train_dn()
        self.train_joint = self._construct_train_joint()

        # Construct a function for computing the ouputs of the generator
        # network for a batch of noise. Presumably, the noise will be drawn
        # from the same distribution that was used in training....
        self.sample_from_gn = self._construct_gn_sampler()
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

    def init_moments(self, X_noise):
        """
        Initialize estimates of the generator distribution's 1st and 2nd-order
        moments based on some large sample of input noise to the generator
        network. Estimates will be performed, and subsequently tracked, in a
        transformed space based on self.mom_match_proj.
        """
        # Compute outputs for the input latent noise in X_noise
        X = self.sample_from_gn(X_noise.astype(theano.config.floatX))
        # Get the transform to apply prior to moment matching
        P = self.mom_match_proj.get_value(borrow=False)
        # Compute post-transform mean and covariance of the outputs
        mu, sigma = projected_moments(X, P, ary_type='numpy')
        # Initialize the generator network's running moment estimates 
        self.GN.dist_cov.set_value(sigma.astype(theano.config.floatX))
        self.GN.dist_mean.set_value(mu.astype(theano.config.floatX))
        return

    def _construct_disc_layers(self, rng):
        """
        Construct binary discrimination layers for each spawn-net in the
        underlying discrimnator pseudo-ensemble. All spawn-nets spawned from
        the same proto-net will use the same disc-layer parameters.
        """
        self.disc_layers = []
        for sn in self.DN.spawn_nets:
            sn_fl = sn[-1]
            self.disc_layers.append(DiscLayer(rng=rng, \
                    input=sn_fl.noisy_input, in_dim=sn_fl.in_dim))
            self.dn_params.extend(self.disc_layers[-1].params)
        return

    def _construct_disc_costs(self):
        """
        Construct the generator and discriminator adversarial costs.
        """
        gn_costs = []
        dn_costs = []
        for d_layer in self.disc_layers:
            dl_output = d_layer.linear_output
            data_preds = dl_output.take(self.Id, axis=0)
            noise_preds = dl_output.take(self.In, axis=0)
            # Compute dn cost based on predictions for both data and noise
            dn_pred_count = self.Id.size + self.In.size
            dnl_dn_cost = (logreg_loss(data_preds, 1.0) + \
                    logreg_loss(noise_preds, -1.0)) / dn_pred_count
            # Compute gn cost based only on predictions for noise
            gn_pred_count = self.In.size
            dnl_gn_cost = ulh_loss(noise_preds, 0.0) / gn_pred_count
            #dnl_gn_cost = logreg_loss(noise_preds, 1.0) / gn_pred_count
            dn_costs.append(dnl_dn_cost)
            gn_costs.append(dnl_gn_cost)
        dn_cost = self.dw_dn[0] * T.sum(dn_costs)
        gn_cost = self.dw_gn[0] * T.sum(gn_costs)
        return [dn_cost, gn_cost]

    def _construct_mom_stuff(self):
        """
        Construct the cost function for the moment-matching "regularizer".
        """
        a = self.mom_mix_rate
        dist_mean = self.GN.dist_mean
        dist_cov = self.GN.dist_cov
        # Get the generated sample observations for this batch, transformed
        # linearly into the desired space for moment matching...
        X_b = T.dot(self.sample_data, self.mom_match_proj)
        # Get their mean
        batch_mean = T.mean(X_b, axis=0)
        # Get the updated generator distribution mean
        new_mean = ((1.0 - a[0]) * self.GN.dist_mean) + (a[0] * batch_mean)
        # Use the mean to get the updated generator distribution covariance
        X_b_minus_mean = X_b - new_mean
        # Whelp, I guess this line needs the cast... for some reason...
        batch_cov = T.dot(X_b_minus_mean.T, X_b_minus_mean) / T.cast(X_b.shape[0], 'floatX')
        new_cov = ((1.0 - a[0]) * self.GN.dist_cov) + (a[0] * batch_cov)
        # Get the cost for deviation from the target distribution's moments
        mean_err = new_mean - self.target_mean
        cov_err = (new_cov - self.target_cov)
        mm_cost = self.mom_match_weight[0] * \
                (T.sum(mean_err**2.0) + T.sum(cov_err**2.0))
        # Construct the updates for the running estimates of the generator
        # distribution's first and second-order moments.
        mom_updates = OrderedDict()
        mom_updates[self.GN.dist_mean] = new_mean
        mom_updates[self.GN.dist_cov] = new_cov
        return [mm_cost, mom_updates]

    def _construct_train_gn(self):
        """
        Construct theano function to train generator on its own.
        """
        outputs = [self.mom_match_cost, self.disc_cost_gn, self.disc_cost_dn]
        func = theano.function(inputs=[ self.Xd, self.Xn, self.Id, self.In ], \
                outputs=outputs, \
                updates=self.gn_updates, \
                givens={self.input_data: self.Xd, \
                        self.input_noise: self.Xn})
        theano.printing.pydotprint(func, \
            outfile='gn_func_graph.png', compact=True, format='png', with_ids=False, \
            high_contrast=True, cond_highlight=None, colorCodes=None, \
            max_label_size=70, scan_graphs=False, var_with_name_simple=False, \
            print_output_file=True, assert_nb_all_strings=-1)
        return func

    def _construct_train_dn(self):
        """
        Construct theano function to train discriminator on its own.
        """
        outputs = [self.mom_match_cost, self.disc_cost_gn, self.disc_cost_dn]
        func = theano.function(inputs=[ self.Xd, self.Xn, self.Id, self.In ], \
                outputs=outputs, \
                updates=self.dn_updates, \
                givens={self.input_data: self.Xd, \
                        self.input_noise: self.Xn})
        theano.printing.pydotprint(func, \
            outfile='dn_func_graph.png', compact=True, format='png', with_ids=False, \
            high_contrast=True, cond_highlight=None, colorCodes=None, \
            max_label_size=70, scan_graphs=False, var_with_name_simple=False, \
            print_output_file=True, assert_nb_all_strings=-1)
        return func

    def _construct_train_joint(self):
        """
        Construct theano function to train generator and discriminator jointly.
        """
        outputs = [self.mom_match_cost, self.disc_cost_gn, self.disc_cost_dn]
        func = theano.function(inputs=[ self.Xd, self.Xn, self.Id, self.In ], \
                outputs=outputs, \
                updates=self.joint_updates, \
                givens={self.input_data: self.Xd, \
                        self.input_noise: self.Xn})
        return func

    def _construct_gn_sampler(self):
        """
        Construct theano function to sample from the gneerator network.
        """
        Xn_sym = T.matrix('gn_sampler_input')
        theano_func = theano.function( \
               inputs=[ Xn_sym ], \
               outputs=[ self.sample_data ], \
               givens={ self.input_noise: Xn_sym })
        sample_func = lambda Xn: theano_func(Xn)[0]
        return sample_func

if __name__=="__main__":
    NOT_DONE = True

    print("TESTING COMPLETE!")




##############
# EYE BUFFER #
##############
