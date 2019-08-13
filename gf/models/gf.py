import math
import numpy as np
import scipy as sp
import scipy.linalg
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.init as init
from torch.distributions import Normal, MultivariateNormal, Distribution, Poisson
from gf.modules.attn import ISAB, PMA, MAB, SAB
from gf.modules.splines import unconstrained_RQS
from gf.models.ep import EdgePredictor
from gf.utils import *


class ActNorm(nn.Module):
    """
    ActNorm layer.
    [Kingma and Dhariwal, 2018.]
    """
    def __init__(self, dim, device):
        super().__init__()
        self.dim = dim
        self.device = device
        self.mu = nn.Parameter(torch.zeros(1, 1, dim, 
            dtype = torch.float, device = self.device))
        self.log_sigma = nn.Parameter(torch.zeros(1, 1, dim, 
            dtype = torch.float, device = self.device))
        self.initialized = False

    def forward(self, x, v):
        z = x * torch.exp(self.log_sigma) + self.mu
        log_det = torch.sum(self.log_sigma).repeat(x.shape[0]) * v
        return z, log_det

    def backward(self, z, v):
        x = (z - self.mu) / torch.exp(self.log_sigma)
        log_det = -torch.sum(self.log_sigma).repeat(z.shape[0]) * v
        return x, log_det


class OneByOneConv(nn.Module):
    """
    Invertible 1x1 convolution.
    [Kingma and Dhariwal, 2018.]
    """
    def __init__(self, dim, device):
        super().__init__()
        self.dim = dim
        self.device = device
        W, _ = sp.linalg.qr(np.random.randn(dim, dim))
        W = torch.tensor(W, dtype=torch.float, device = device)
        self.W = nn.Parameter(W)
        self.W_inv = None

    def forward(self, x, v):
        z = x @ self.W
        log_det = torch.slogdet(self.W)[1].repeat(x.shape[0]) * v
        return z, log_det

    def backward(self, z, v):
        if self.W_inv is None:
            self.W_inv = torch.inverse(self.W)
        x = z @ self.W_inv
        log_det = -torch.slogdet(self.W)[1].repeat(z.shape[0]) * v
        return x, log_det


class MLP(nn.Module):
    """
    Simple fully connected neural network.
    """
    def __init__(self, in_dim, out_dim, hidden_dim):
        super().__init__()
        self.network = nn.Sequential(
            nn.Linear(in_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, out_dim),
        )

    def forward(self, x):
        return self.network(x)


class GFLayerNSF(nn.Module):
    """
    Neural spline flow, coupling layer.
    """
    def __init__(self, embedding_dim, device,
                 K = 5, B = 3, hidden_dim = 64, base_network = MLP):
        super().__init__()
        self.embedding_dim = embedding_dim
        self.device = device
        self.K = K
        self.B = B
        self.f1 = ISAB(embedding_dim // 2, hidden_dim, 1, 16)
        self.f2 = ISAB(embedding_dim // 2, hidden_dim, 1, 16)
        self.base_network = base_network(hidden_dim, (3 * K - 1) * embedding_dim // 2, hidden_dim)
        self.conv = OneByOneConv(embedding_dim, device)
        self.actnorm = ActNorm(embedding_dim, device)

    def forward(self, x, v):
        batch_size, max_n_nodes = x.shape[0], x.shape[1]
        mask = construct_embedding_mask(x, v)
        x, log_det = self.actnorm(x, v)
        x, ld = self.conv(x, v)
        log_det += ld
        lower = x[:, :, :self.embedding_dim // 2]
        upper = x[:, :, self.embedding_dim // 2:]
        out = self.base_network(self.f1(lower, mask.byte())).reshape(
            batch_size, -1, self.embedding_dim // 2, 3 * self.K - 1)
        W, H, D = torch.split(out, self.K, dim = 3)
        W, H = torch.softmax(W, dim = 3), torch.softmax(H, dim = 3)
        W, H = 2 * self.B * W, 2 * self.B * H
        D = F.softplus(D)
        upper, ld = unconstrained_RQS(
            upper, W, H, D, inverse=False, tail_bound=self.B)
        log_det += torch.sum(ld * mask.unsqueeze(2), dim = (1, 2))
        out = self.base_network(self.f2(upper, mask.byte())).reshape(
            batch_size, -1, self.embedding_dim // 2, 3 * self.K - 1)
        W, H, D = torch.split(out, self.K, dim = 3)
        W, H = torch.softmax(W, dim = 3), torch.softmax(H, dim = 3)
        W, H = 2 * self.B * W, 2 * self.B * H
        D = F.softplus(D)
        lower, ld = unconstrained_RQS(
            lower, W, H, D, inverse=False, tail_bound=self.B)
        log_det += torch.sum(ld * mask.unsqueeze(2), dim = (1, 2))
        return torch.cat([lower, upper], dim = 2), log_det

    def backward(self, z, v):
        batch_size, max_n_nodes = z.shape[0], z.shape[1]
        mask = construct_embedding_mask(z, v)
        log_det = torch.zeros_like(z)
        lower = z[:, :, :self.embedding_dim // 2]
        upper = z[:, :, self.embedding_dim // 2:]
        out = self.base_network(self.f2(upper, mask.byte())).reshape(
            batch_size, -1, self.embedding_dim // 2,  3 * self.K - 1)
        W, H, D = torch.split(out, self.K, dim = 3)
        W, H = torch.softmax(W, dim = 3), torch.softmax(H, dim = 3)
        W, H = 2 * self.B * W, 2 * self.B * H
        D = F.softplus(D)
        lower, ld = unconstrained_RQS(
            lower, W, H, D, inverse=True, tail_bound=self.B)
        log_det += torch.sum(ld * mask.unsqueeze(2), dim = (1, 2))
        out = self.base_network(self.f1(lower, mask.byte())).reshape(
            batch_size, -1, self.embedding_dim // 2, 3 * self.K - 1)
        W, H, D = torch.split(out, self.K, dim = 3)
        W, H = torch.softmax(W, dim = 3), torch.softmax(H, dim = 3)
        W, H = 2 * self.B * W, 2 * self.B * H
        D = F.softplus(D)
        upper, ld = unconstrained_RQS(
            upper, W, H, D, inverse=True, tail_bound=self.B)
        log_det += torch.sum(ld *  mask.unsqueeze(2), dim = (1, 2))
        x, ld1 = self.conv.backward(torch.cat([lower, upper], dim = 2), v)
        x, ld2 = self.actnorm.backward(x, v)
        return x, log_det + ld1 + ld2

class GF(nn.Module):

    def __init__(self, embedding_dim, num_flows, device):
        super().__init__()
        self.embedding_dim = embedding_dim
        self.flows_L = nn.ModuleList([GFLayerNSF(embedding_dim, device) \
                                      for _ in range(num_flows)])
        self.flows_Z = nn.ModuleList([GFLayerNSF(embedding_dim, device) \
                                      for _ in range(num_flows)])
        self.device = device
        self.n_nodes_lambda = nn.Parameter(torch.ones(1, device = device))
        self.ep = EdgePredictor(embedding_dim, device)
        self.final_actnorm = ActNorm(embedding_dim, device)

    def sample_prior(self, n_batch):
        #n_nodes = Poisson(self.n_nodes_lambda).sample((1,)).squeeze().squeeze()
        n_nodes = 15
        prior = Normal(loc = torch.zeros(n_nodes * self.embedding_dim, 
                                         device = self.device),
                       scale = torch.ones(n_nodes * self.embedding_dim, 
                                          device = self.device))
        Z = prior.sample((n_batch,))
        Z = Z.reshape((n_batch, n_nodes, self.embedding_dim))
        V = torch.ones(n_batch) * n_nodes
        return Z, V

    def forward(self, X, A, V):
        """
        Returns:
            log probability per node
        """
        batch_size, max_n_nodes = X.shape[0], X.shape[1]
        log_det = torch.zeros(batch_size, device=self.device)
        for flow in self.flows_L:
            X, LD = flow.forward(X, V)
            log_det += LD
        ep_loss = self.ep.loss(X, A, V)
        for flow in self.flows_Z:
            X, LD = flow.forward(X, V)
            log_det += LD
        X, LD = self.final_actnorm(X, V)
        log_det += LD
        prior = Normal(loc = torch.zeros(max_n_nodes * self.embedding_dim, 
                                         device = self.device),
                       scale = torch.ones(max_n_nodes * self.embedding_dim, 
                                          device = self.device))
        Z, prior_logprob = X, prior.log_prob(X.view(batch_size, -1))
        prior_logprob = prior_logprob.reshape((batch_size, max_n_nodes, -1))
        mask = torch.zeros(batch_size, max_n_nodes, device = self.device)
        for i, cnt in enumerate(V):
            mask[i, :cnt.int()] = 1
        prior_logprob = torch.sum(prior_logprob * mask.unsqueeze(2), dim = (1, 2))
        #self.n_nodes_lambda = torch.mean(V)
        return Z, (prior_logprob + log_det) / V - ep_loss 

    def predict_A_from_E(self, X, V):
        batch_size, n_nodes = X.shape[0], X.shape[1]
        for flow in self.flows_L:
            X,  _ = flow.forward(X, V)
        return torch.sigmoid(self.ep.forward(X, V))

    def backward(self, Z, V):
        B, N, _ = Z.shape
        Z, _ = self.final_actnorm.backward(Z, V)
        for flow in self.flows_Z[::-1]:
            Z, _ = flow.backward(Z, V)
        return Z

