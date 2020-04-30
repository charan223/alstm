"""adaptive RNN

PyTorch implementation of an RNN version of the adaptive LSTM (https://arxiv.org/abs/1805.08574).
"""
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn import Parameter
from torch.autograd import Variable
# from torch.nn._functions.thnn import rnnFusedPointwise as fusedBackend

from .utils import Project, VariationalDropout, chunk, convert, get_sizes, init_hidden

# pylint: disable=too-many-locals,too-many-arguments,too-many-instance-attributes,redefined-builtin


def arnn_cell(input, hidden, policy, weight_ih, weight_hh, bias=None, activation=torch.tanh):
    r"""Update hidden state for an aRNN at given time step:

    :math:`h_t = \operatorname{arnn}_{\theta}(x_t, h_{t-1}, \pi_t)`.

    Args:
        input (Tensor): Batch of inputs [batch_size, input_size]
        hidden (Tensor): Batch of hidden states [batch_size, hidden_size]
        policy (Tensor): Batch of adaptation values [batch_size, *]
            where * = input_size + hidden_size if no bias else
            input_size + 2 * hidden_size
        weight_ih (Tensor): Input projection [hidden_size, input_size]
        weight_hh (Tensor): Hidden projection [hidden_size, hidden_size]
        bias (Tensor): Bias (optional) [hidden_size]

    Returns:
        hy (Tensor): hidden state
    """
    hx = hidden

    hidden_size, input_size = hx.size(1), input.size(1)
    chunks = [input_size, hidden_size, hidden_size, hidden_size, hidden_size]
    if bias is not None:
        chunks.append(hidden_size)

    policy = chunk(policy, chunks, 1)

    input = input * policy.pop(0)
    hx = hx * policy.pop(0)
    igates = F.linear(input, weight_ih) * policy.pop(0)
    hgates = F.linear(hx, weight_hh) * policy.pop(0)

    if bias is not None:
        igates = igates + bias * policy.pop(0)

    gates = igates + hgates
    hy = activation(gates)
    return hy


class aRNNCell(nn.modules.rnn.RNNCellBase):

    r"""Module wrapper around aRNN cell:

    :math:`h_t = \operatorname{arnn}_{\theta}(x_t, h_{t-1}, \pi_t)`.

    Args:
        input_size (int): dimensionality of input
        hidden_size (int): dimensionality of hidden state
        use_bias (bool): apply bias
    """

    def __init__(self, input_size, hidden_size, use_bias=True):
        super(aRNNCell, self).__init__(input_size, hidden_size, bias=use_bias, num_chunks=1)
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.use_bias = use_bias
        self.weight_ih = Parameter(torch.Tensor(hidden_size, input_size))
        self.weight_hh = Parameter(torch.Tensor(hidden_size, hidden_size))
        if use_bias:
            self.bias = Parameter(torch.Tensor(hidden_size))
        else:
            self.register_parameter('bias', None)
        self.reset_parameters()

    def reset_parameters(self):
        """Initialization of parameters"""
        # Hack for handling PyTorch v4 moving to orthogonal_
        f = getattr(torch.nn.init, 'orthogonal_', torch.nn.init.orthogonal)
        f(self.weight_ih)
        f(self.weight_hh)
        if self.use_bias:
            self.bias.data.zero_()

    def forward(self, input, hx, policy):
        r"""Update hidden state for given time step:

        :math:`h_t = \operatorname{arnn}_{\theta}(x_t, h_{t-1}, \pi_t)`.

        Args:
            input (Tensor): Batch of inputs [batch_size, input_size]
            hx (Tensor): Batch  of hidden states [batch_size, hidden_size]
            policy (Tensor): Batch of adaptation values [batch_size, *]
                where * = input_size + hidden_size if no bias else
                input_size + 2 * hidden_size
        """
        return arnn_cell(
            input, hx, policy, self.weight_ih, self.weight_hh, self.bias)


class aRNN(nn.Module):

    r"""aRNN model

    Model for processing a batch of sequence inputs over a
    potentially deep aRNN. The :class:`aRNN` provides a unified
    framework for the aRNN, a version of the aLSTM (https://arxiv.org/abs/1805.08574). It
    uses variational dropout and hybrid RHN-RNN adaptation when the
    model has more than one layer.

    Args:
        input_size (int): dimensionality of input
        hidden_size (int): dimensionality of hidden state
        adapt_size (int): dimensionality of latent adaptation variable
        output_size (int): dimensionality of hidden state in final layer (optional)
        nlayers (int): number of layers (default: 1)
        dropout_alstm (int): drop probability for aLSTM hidden state (optional)
        dropout_adapt (int): drop probability for adaptive latent variable (optional)
        batch_first (int): whether batches are along first dimension (default: False)
        bias (bool): whether to use a bias (default: True)

    Input:
        input (Tensor): Batch of sequences [batch_size, max_seq_len, input_size]
        hidden (Tensor): Two lists of hidden states (optional) [batch_size, hidden_size]

    Returns:
        output (Tensor): Batch of sequences [batch_size, max_seq_len, output_size]
        hidden (Tensor): Two lists of hidden states [batch_size, hidden_size]
    """

    def __init__(self, input_size, hidden_size, adapt_size, output_size=None,
                 nlayers=1, dropout_arnn=None, dropout_adapt=None,
                 batch_first=False, bias=True):
        super(aRNN, self).__init__()
        output_size = output_size if output_size is not None else hidden_size
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.adapt_size = adapt_size
        self.output_size = output_size
        self.nlayers = nlayers
        self.dropout_arnn = dropout_arnn
        self.dropout_adapt = dropout_adapt
        self.batch_first = batch_first
        self.bias = bias

        psz, alyr, elyr, flyr = [], [], [], []
        for l in range(nlayers):
            ninp, nhid = get_sizes(
                input_size, hidden_size, output_size, l, nlayers)

            # policy latent variable
            ain = adapt_size + ninp + nhid if nlayers != 1 else ninp + nhid
            alyr.append(nn.LSTMCell(ain, adapt_size))

            # sub-policy projection
            ipsz = ninp + nhid
            opsz = 3 * nhid if not bias else 4 * nhid
            psz.append(ipsz + opsz)
            elyr.append(Project(adapt_size, psz[-1]))

            # aRNN
            flyr.append(aRNNCell(ninp, nhid, use_bias=bias))

        self.adapt_layers = nn.ModuleList(alyr)
        self.project_layers = nn.ModuleList(elyr)
        self.arnn_layers = nn.ModuleList(flyr)
        self.policy_sizes = psz

    def forward(self, input, hidden=None, return_all=False):
        """Run aLSTM over a batch of input sequences

        Args:
            input (Tensor): Batch of inputs [batch_size, max_seq_len, input_size]
            hidden (Tensor): Two lists of hidden states (optional) [batch_size, hidden_size]
            return_all (Bool): whether to return also outputs (incl. non-dropped) from all layers

        Returns:
            output (Tensor): Batch of sequences [batch_size, max_seq_len, output_size]
            hidden (Tensor): Two lists of hidden states [batch_size, hidden_size]
            raw_outputs (Tensor): all non-dropped hidden states from all layers
            all_outputs (Tensor): all dropped hidden states from all layers
        """
        if self.batch_first:
            input = input.transpose(0, 1)

        if hidden is None:
            hidden = self.init_hidden(input.size(1))
        hidden = convert(hidden, list)

        adapt_hidden, arnn_hidden = hidden

        dropout_arnn = self._dropout(arnn_hidden, self.dropout_arnn)
        dropout_adapt = self._dropout(adapt_hidden, self.dropout_adapt)

        output, output_all, output_all_raw = [], [], []
        for x in input:
            for l in range(self.nlayers):
                alyr = self.adapt_layers[l]
                elyr = self.project_layers[l]
                flyr = self.arnn_layers[l]
                ahx, ahc = adapt_hidden[l]
                fhx, fhc = arnn_hidden[l]
                if self.nlayers != 1:
                    ax = torch.cat([x, fhx, adapt_hidden[l-1][0]], 1)
                else:
                    ax = torch.cat([x, fhx], 1)

                ahx, ahc = alyr(ax, (ahx, ahc))
                ahx = dropout_adapt(ahx, l)
                ahe = elyr(ahx)

                fhx_nd = flyr(x, fhx, ahe)
                if l == self.nlayers - 1:
                    output.append(fhx_nd)

                fhx = dropout_arnn(fhx_nd, l)

                if return_all:
                    output_all_raw.append(fhx_nd)
                    output_all.append(fhx)

                adapt_hidden[l] = [ahx, ahc]
                arnn_hidden[l] = [fhx, fhc]

                x = fhx
            ###
        ###
        hidden = (adapt_hidden, arnn_hidden)
        output = torch.stack(output, 1 if self.batch_first else 0)

        hidden = convert(hidden, tuple)
        if return_all:
            return output, hidden, output_all, output_all_raw
        return output, hidden

    def init_hidden(self, bsz):
        """Utility for initializing hidden states (to zero)"""
        asz = self.adapt_size
        osz = self.output_size
        hsz = self.hidden_size
        data_source = next(self.parameters()).data

        return init_hidden(data_source, bsz, asz, osz, hsz, self.nlayers)

    def _dropout(self, hiddens, dropout_rates):
        data_source = next(self.parameters()).data
        if self.training and dropout_rates:
            msz = [h[0].size() for h in hiddens]
            return VariationalDropout(data_source, dropout_rates, msz)
        return lambda x, l: x
