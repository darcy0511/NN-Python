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
from EarNet import HiddenLayer


#####################################
# GENERATIVE NETWORK IMPLEMENTATION #
#####################################


class GEN_NET(object):
    """
    A net that transforms a simple distribution so that it matches some
    more complicated distribution, for some definition of match....

    Parameters:
        rng: a numpy.random RandomState object
        input_noise: symbolic input matrix for inputting latent noise
        input_data: symbolic input matrix for inputting real data
        params: a dict of parameters describing the desired ensemble:
            use_bias: whether to uses biases in hidden and output layers
            lam_l2a: L2 regularization weight on neuron activations
            vis_drop: drop rate to use on samples from the base distribution
            hid_drop: drop rate to use on activations of hidden layers
                -- note: vis_drop/hid_drop are optional, with defaults 0.0/0.0
            bias_noise: standard dev for noise on the biases of hidden layers
            out_noise: standard dev for noise on the output of this net
            mlp_config: list of "layer descriptions"
    """
    def __init__(self,
            rng=None,
            input_noise=None,
            input_data=None,
            params=None):
        # First, setup a shared random number generator for this layer
        self.rng = theano.tensor.shared_randomstreams.RandomStreams( \
            rng.randint(100000))
        # Grab the symbolic input matrix
        self.input_noise = input_noise
        self.input_data = input_data
        #####################################################
        # Process user-supplied parameters for this network #
        #####################################################
        lam_l2a = params['lam_l2a']
        use_bias = params['use_bias']
        if 'vis_drop' in params:
            self.vis_drop = params['vis_drop']
        else:
            self.vis_drop = 0.0
        if 'hid_drop' in params:
            self.hid_drop = params['hid_drop']
        else:
            self.hid_drop = 0.0
        if 'bias_noise' in params:
            self.bias_noise = params['bias_noise']
        else:
            self.bias_noise = 0.0
        if 'out_noise' in params:
            self.out_noise = params['out_noise']
        else:
            self.out_noise = 0.0
        # Get the configuration/prototype for this network. The config is a
        # list of layer descriptions, including a description for the input
        # layer, which is typically just the dimension of the inputs. So, the
        # depth of the mlp is one less than the number of layer configs.
        self.mlp_config = params['mlp_config']
        self.mlp_depth = len(self.mlp_config) - 1
        self.latent_dim = self.mlp_config[0]
        self.data_dim = self.mlp_config[-1]
        ##########################
        # Initialize the network #
        ##########################
        self.clip_params = {}
        self.mlp_layers = []
        layer_def_pairs = zip(self.mlp_config[:-1],self.mlp_config[1:])
        layer_num = 0
        next_input = self.input_noise
        for in_def, out_def in layer_def_pairs:
            first_layer = (layer_num == 0)
            last_layer = (layer_num == (len(layer_def_pairs) - 1))
            l_name = "gn_layer_{0:d}".format(layer_num)
            if (type(in_def) is list) or (type(in_def) is tuple):
                # Receiving input from a poolish layer...
                in_dim = in_def[0]
            else:
                # Receiving input from a normal layer...
                in_dim = in_def
            if (type(out_def) is list) or (type(out_def) is tuple):
                # Applying some sort of pooling in this layer...
                out_dim = out_def[0]
                pool_size = out_def[1]
            else:
                # Not applying any pooling in this layer...
                out_dim = out_def
                pool_size = 0
            # Select the appropriate noise to add to this layer
            if first_layer:
                d_rate = self.vis_drop
            else:
                d_rate = self.hid_drop
            if last_layer:
                b_noise = self.out_noise
            else:
                b_noise = self.bias_noise
            # Add a new, well-configured layer to the regular model
            self.mlp_layers.append(HiddenLayer(rng=rng, \
                    input=next_input, activation=None, pool_size=pool_size, \
                    drop_rate=d_rate, input_noise=0., bias_noise=b_noise, \
                    in_dim=in_dim, out_dim=out_dim, use_bias=use_bias, \
                    name=l_name, W_scale=4.0))
            next_input = self.mlp_layers[-1].output
            # Set the non-bias parameters of this layer to be clipped
            self.clip_params[self.mlp_layers[-1].W] = 1
            # Acknowledge layer completion
            layer_num = layer_num + 1

        # set norms to which to clip various parameters
        self.clip_norms = {}

        # Mash all the parameters together, into a list.
        self.mlp_params = []
        for layer in self.mlp_layers:
            self.mlp_params.extend(layer.params)

        # The output of this generator network is given by the noisy output
        # of its final layer. We will keep a running estimate of the mean and
        # covariance of the distribution induced by combining this network's
        # latent noise source with its deep non-linear transform. These will
        # be used to encourage the induced distribution to match the first and
        # second-order moments of the distribution we are trying to match.
        #self.output_noise = self.mlp_layers[-1].noisy_linear
        self.output_noise = self.mlp_layers[-1].noisy_linear
        self.out_dim = self.mlp_layers[-1].out_dim
        C_init = np.zeros((self.out_dim,self.out_dim)).astype(theano.config.floatX)
        m_init = np.zeros((self.out_dim,)).astype(theano.config.floatX)
        self.dist_mean = theano.shared(m_init, name='gn_dist_mean')
        self.dist_cov = theano.shared(C_init, name='gn_dist_cov')
        # Get simple regularization penalty to moderate activation dynamics
        self.act_reg_cost = lam_l2a * self._act_reg_cost()
        # Joint the transformed noise output and the real data input
        self.output = T.vertical_stack(self.input_data, self.output_noise)
        return

    def _act_reg_cost(self):
        """Apply L2 regularization to the activations in each spawn-net."""
        act_sq_sums = []
        for layer in self.mlp_layers:
            act_sq_sums.append(layer.act_l2_sum)
        full_act_sq_sum = T.sum(act_sq_sums)
        return full_act_sq_sum

    def _batch_moments(self):
        """
        Compute covariance and mean of the current sample outputs.
        """
        mu = T.mean(self.output_noise, axis=0, keepdims=True)
        sigma = T.dot((self.output_noise.T - mu.T), (self.output_noise - mu))
        return [mu, sigma]

    def init_moments(self, X_noise):
        """
        Initialize the running mean and covariance estimates.
        """
        X_noise_sym = T.dmatrix()
        out_func = theano.function(inputs=[ X_noise_sym ], \
                outputs=[ self.output_noise ], \
                givens={self.input_noise: X_noise_sym})
        # Compute outputs for the input latent noise matrix
        X_out = out_func(X_noise)[0]
        # Compute mean and covariance of the outputs
        mu = np.mean(X_out, axis=0)
        X_out_minus_mu = X_out - mu
        sigma = np.dot(X_out_minus_mu.T,X_out_minus_mu) / X_out.shape[0]
        # Initialize the network's running estimates 
        self.dist_cov.set_value(sigma.astype(theano.config.floatX))
        self.dist_mean.set_value(mu.astype(theano.config.floatX))
        return

######################################
# SIMPLE BINARY DISCRIMINATION LAYER #
######################################

def logreg_loss(Y, class_sign):
    """
    Simple binomial deviance (i.e. logistic regression) loss.

    This assumes that all predictions in Y have the same target class, which
    is indicated by class_sign, which should be in {-1, +1}. Note: this does
    not "normalize" for the number of predictions in Y.
    """
    loss = T.sum(T.log(1.0 + T.exp(-class_sign * Y)))
    return loss

class DiscLayer(object):
    def __init__(self, rng, input, in_dim, W=None, b=None):
        # Setup a shared random generator for this layer
        self.rng = theano.tensor.shared_randomstreams.RandomStreams( \
                rng.randint(100000))
        #self.rng = CURAND_RandomStreams(rng.randint(1000000))

        self.input = input
        self.in_dim = in_dim

        # Get some random initial weights and biases, if not given
        if W is None:
            # Generate random initial filters in a typical way
            W_init = 0.01 * np.asarray(rng.normal( \
                      size=(self.in_dim, 1)), \
                      dtype=theano.config.floatX)
            W = theano.shared(value=W_init)
        if b is None:
            b_init = np.zeros((1,), dtype=theano.config.floatX)
            b = theano.shared(value=b_init)

        # Set layer weights and biases
        self.W = W
        self.b = b

        # Compute linear "pre-activation" for this layer
        self.linear_output = T.dot(self.input, self.W) + self.b

        # Apply activation function
        self.output = self.linear_output

        # Conveniently package layer parameters
        self.params = [self.W, self.b]
        # little layer construction complete...
        return

    def _noisy_params(self, P, noise_lvl=0.):
        """Noisy weights, like convolving energy surface with a gaussian."""
        P_nz = P + self.rng.normal(size=P.shape, avg=0.0, std=noise_lvl, \
                dtype=theano.config.floatX)
        return P_nz


class GA_PAIR(object):
    """
    Controller for training a generator/discriminator pair.

    The generator is currently based on the the GEN_NET class implemented in
    this source file. The discriminator must be an instance of the EAR_NET
    class, as implemented in "EarNet.py".

    Parameters:
        rng: numpy.random.RandomState (for reproducibility)
        d_net: The EAR_NET instance that will serve as the discriminator
        g_net: The GEN_NET instance that will serve as the generator
        gap_params: a dict of parameters for controlling various costs
            mom_mix_rate: rate for updates to the running moment estimates
                          for the distribution generated by g_net
            mom_match_weight: weight for the "moment matching" cost
            target_mean: first-order moment to try and match with g_net
            target_cov: second-order moment to try and match with g_net
    """
    def __init__(self, rng=None, d_net=None, g_net=None, params=None):
        # Do some stuff!
        self.rng = theano.tensor.shared_randomstreams.RandomStreams( \
                rng.randint(100000))
        self.DN = d_net
        self.GN = g_net
        self.input_noise = self.GN.input_noise
        self.input_data = self.GN.input_data
        self.latent_dim = self.GN.latent_dim
        self.data_dim = self.GN.data_dim

        # symbolic var data input
        self.Xd = T.dmatrix(name='gap_Xd')
        # symbolic var noise input
        self.Xn = T.dmatrix(name='gap_Xn')
        # symbolic matrix of indices for data inputs
        self.Id = T.lvector(name='gap_Id')
        # symbolic matrix of indices for noise inputs
        self.In = T.lvector(name='gap_In')
        # shared var learning rate for generator and discriminator
        zero_ary = np.zeros((1,)).astype(theano.config.floatX)
        self.lr_gn = theano.shared(value=zero_ary, name='gap_lr_gn')
        self.lr_dn = theano.shared(value=zero_ary, name='gap_lr_dn')
        # shared var momentum parameters for generator and discriminator
        self.mo_gn = theano.shared(value=zero_ary, name='gap_mo_gn')
        self.mo_dn = theano.shared(value=zero_ary, name='gap_mo_dn')
        # shared var weights for adversarial classification objective
        self.dw_gn = theano.shared(value=zero_ary, name='gap_dw_gn')
        self.dw_dn = theano.shared(value=zero_ary, name='gap_dw_dn')
        # init parameters for controlling learning dynamics
        self.set_gn_sgd_params() # init SGD rate/momentum for GN
        self.set_dn_sgd_params() # init SGD rate/momentum for DN
        self.set_disc_weights()  # init adversarial cost weights for GN/DN

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
        self.mom_mix_rate = params['mom_mix_rate']
        self.mom_match_weight = params['mom_match_weight']
        targ_mean = params['target_mean'].astype(theano.config.floatX)
        targ_cov = params['target_cov'].astype(theano.config.floatX)
        assert(targ_mean.size == targ_cov.shape[0]) # mean and cov use same dim
        assert(targ_cov.shape[0] == targ_cov.shape[1]) # cov must be square
        self.target_mean = theano.shared(value=targ_mean, name='gap_target_mean')
        self.target_cov = theano.shared(value=targ_cov, name='gap_target_cov')
        mmp = np.identity(targ_cov.shape[0]) # default to identity transform
        if 'mom_match_proj' in params:
            mmp = params['mom_match_proj'] # use a user-specified transform
        print("mmp.shape[0]={0:d}, self.data_dim={1:d}".format(mmp.shape[0], self.data_dim))
        assert(mmp.shape[0] == self.data_dim) # transform matches data dim
        assert(mmp.shape[1] == targ_cov.shape[0]) # and matches mean/cov dims
        self.mom_match_proj = theano.shared(value=mmp, name='gap_mom_map_proj')
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
        # because the GA_PAIR requires that they be "bypassed" in favor of some
        # binary classification layers that will be managed by this GA_PAIR.
        self.dn_params = []
        for pn in self.DN.proto_nets:
            for pnl in pn[0:-1]:
                self.dn_params.extend(pnl.params)
        self.gn_params = [p for p in self.GN.mlp_params]
        # Now construct a binary discriminator layer for each proto-net in the
        # discriminator network. And, add their params to optimization list.
        self._construct_disc_layers(rng)

        # Construct costs for the generator and discriminator networks based 
        # on adversarial binary classification
        self.disc_cost_dn, self.disc_cost_gn = self._construct_disc_costs()

        # Cost w.r.t. discriminator parameters is only the adversarial binary
        # classification cost. Cost w.r.t. comprises an adversarial binary
        # classification cost and the (weighted) moment matching cost.
        self.dn_cost = self.disc_cost_dn
        self.gn_cost = self.disc_cost_gn + self.mom_match_cost

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
        zero_ary = np.zeros((1,)).astype(theano.config.floatX)
        new_lr = zero_ary + learn_rate
        self.lr_gn.set_value(new_lr)
        new_mo = zero_ary + momentum
        self.mo_gn.set_value(new_mo)
        return

    def set_dn_sgd_params(self, learn_rate=0.02, momentum=0.9):
        """
        Set learning rate and momentum parameter for discriminator updates.
        """
        zero_ary = np.zeros((1,)).astype(theano.config.floatX)
        new_lr = zero_ary + learn_rate
        self.lr_dn.set_value(new_lr)
        new_mo = zero_ary + momentum
        self.mo_dn.set_value(new_mo)
        return

    def init_moments(self, X_noise):
        """
        Initialize estimates of the generator distribution's 1st and 2nd-order
        moments based on some large sample of input noise to the generator
        network. Estimates will be performed, and subsequently tracked, in a
        transformed space based on self.mom_match_proj.
        """
        # Compute outputs for the input latent noise in X_noise
        X = self.sample_from_gn(X_noise)
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
            dnl_gn_cost = logreg_loss(noise_preds, 1.0) / gn_pred_count
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
        X_b = T.dot(self.GN.output_noise, self.mom_match_proj)
        # Get their mean
        batch_mean = T.mean(X_b, axis=0)
        # Get the updated generator distribution mean
        new_mean = ((1.0 - a) * self.GN.dist_mean) + (a * batch_mean)
        # Use the mean to get the updated generator distribution covariance
        X_b_minus_mean = X_b - new_mean
        batch_cov = T.dot(X_b_minus_mean.T, X_b_minus_mean) / X_b.shape[0]
        new_cov = ((1.0 - a) * self.GN.dist_cov) + (a * batch_cov)
        # Get the cost for deviation from the target distribution's moments
        mean_err = new_mean - self.target_mean
        cov_err = (new_cov - self.target_cov)
        mm_cost = self.mom_match_weight * \
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
        Xn_sym = T.dmatrix('gn_sampler_input')
        theano_func = theano.function( \
               inputs=[ Xn_sym ], \
               outputs=[ self.GN.output_noise ], \
               givens={ self.GN.input_noise: Xn_sym })
        sample_func = lambda Xn: theano_func(Xn)[0]
        return sample_func

#############################################
# HELPER FUNCTION FOR 1st/2nd ORDER MOMENTS #
#############################################

def projected_moments(X, P, ary_type=None):
    """
    Compute 1st/2nd-order moments after linear transform.

    Return type is always a numpy array. Inputs should both be of the same
    type, which can be either numpy array or theano shared variable.
    """
    assert(not (ary_type is None))
    assert((ary_type == 'theano') or (ary_type == 'numpy'))
    proj_mean = None
    proj_cov = None
    if ary_type == 'theano':
        Xp = T.dot(X, P)
        Xp_mean = T.mean(Xp, axis=0)
        Xp_centered = Xp - Xp_mean
        Xp_cov = T.dot(Xp_centered.T, Xp_centered) / Xp.shape[0]
        proj_mean = Xp_mean.eval()
        proj_cov = Xp_cov.eval()
    else:
        Xp = np.dot(X, P)
        Xp_mean = np.mean(Xp, axis=0)
        Xp_centered = Xp - Xp_mean
        Xp_cov = np.dot(Xp_centered.T, Xp_centered) / Xp.shape[0]
        proj_mean = Xp_mean
        proj_cov = Xp_cov
    return [proj_mean, proj_cov]

if __name__=="__main__":
    import time
    from load_data import load_udm, load_udm_ss, load_mnist
    from EarNet import EAR_NET
    # Simple test code, to check that everything is basically functional.
    print("TESTING...")

    # Initialize a source of randomness
    rng = np.random.RandomState(1234)

    # Load some data to train/validate/test with
    dataset = 'data/mnist.pkl.gz'
    datasets = load_udm(dataset, zero_mean=False)
    Xtr = datasets[0][0]
    mu = T.mean(Xtr,axis=0,keepdims=True)
    sigma = T.dot((Xtr.T - mu.T),(Xtr - mu))
    Xtr_mean = mu.eval()
    Xtr_cov = sigma.eval()

    # Choose some parameters for the generative network
    gn_params = {}
    gn_config = [50, 200, 200, 28*28]
    gn_params['mlp_config'] = gn_config
    gn_params['lam_l2a'] = 1e-2
    gn_params['use_bias'] = 1
    gn_params['vis_drop'] = 0.0
    gn_params['hid_drop'] = 0.0
    gn_params['bias_noise'] = 0.1
    gn_params['out_noise'] = 0.1

    # Symbolic input matrix to generator network
    X_gn_sym = T.dmatrix(name='X_gn_sym')
    X_gn_noise = T.dmatrix(name='X_gn_noise')

    # Initialize a generator network object
    GN = GEN_NET(rng, X_gn_sym, gn_params)

    # Init GNET's mean and covariance estimates with many samples
    X_noise = npr.randn(5000, GN.latent_dim)
    GN.init_moments(X_noise)

    gn_out_func = theano.function(inputs=[ X_gn_noise ], \
            outputs=[ GN.output ], \
            givens={X_gn_sym: X_gn_noise})

    batch_count = 100
    start_time = time.clock()
    for i in range(batch_count):
        X_noise = npr.randn(100, GN.latent_dim)
        outputs = gn_out_func(X_noise)
        X_gn_out = outputs[0]
        print("X_gn_out.shape: {0:s}".format(X_gn_out.shape))
    total_time = time.clock() - start_time
    print("SPEED: {0:.4f}s per batch".format(total_time/batch_count))


    print("TESTING COMPLETE!")




##############
# EYE BUFFER #
##############