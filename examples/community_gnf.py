import logging
import itertools
import torch
import torch.nn as nn
import torch.optim as optim
import numpy as np
import networkx as nx
from argparse import ArgumentParser
from matplotlib import pyplot as plt
from graphflows.gnf import GRevNet, GAE, EdgePredictor
from graphflows.graphflow import GF
from graphflows.attn import *
from graphflows.fgsd import compute_fgsd_embeddings
from tqdm import tqdm


def gen_graphs(sizes, p_intra=0.7, p_inter=0.01):
    """
    Generate community graphs.
    """
    max_size = np.max(sizes)
    X = np.zeros((len(sizes), max_size, 32))
    A = np.zeros((len(sizes), max_size, max_size))
    for idx, V in enumerate(sizes):
        comms = [nx.gnp_random_graph(V // 2, p_intra),
                 nx.gnp_random_graph((V + 1) // 2, p_intra)]
        graph = nx.disjoint_union_all(comms)
        graph = nx.to_numpy_array(graph)
        block1 = np.arange(V // 2)
        block2 = np.arange(V // 2, V)
        remaining = list(itertools.product(block1, block2))
        np.random.shuffle(remaining)
        for (i, j) in remaining[:int(p_inter * V + 1)]:
            graph[i,j] = 1
            graph[j,i] = 1
        P = np.eye(V)
        np.random.shuffle(P)
        graph = P.T @ graph @ P
        graph = np.pad(graph, (0, max_size - V), "constant")
        X[idx,:] = np.r_[0.5 * np.random.randn(V, 32),
                         np.zeros((max_size - V, 32))]
        A[idx] = graph
    return X, A


def convert_pairwise(A, E):
    """
    Convert to a representation with a pairwise relationship between each node.

    First and second row represent embeddings for the pair in question.
    Rest of the rows represent embeddings for all remaining pairs.
    """
    X, Y, idxs = [], [], []
    for A_k, E_k in tqdm(zip(A, E), total=len(A)):
        for (i, j) in itertools.combinations(np.arange(len(A_k)), 2):
            Y += [A_k[i, j]]
            first = E_k[i][np.newaxis,:]
            second = E_k[j][np.newaxis,:]
            rest_idx = np.r_[np.arange(i), np.arange(i + 1, j),
                             np.arange(j + 1, len(A_k))]
            rest = np.take(E_k, rest_idx, axis=0)
            X += [np.r_[first, second, rest]]
            idxs+= [(i, j)]
    return idxs, np.array(X), np.array(Y)


def construct_pairwise_X(E):
    X, idxs = [], []
    for E_k in tqdm(E, total=len(E)):
        for (i, j) in itertools.combinations(np.arange(len(E_k)), 2):
            first = E_k[i][np.newaxis,:]
            second = E_k[j][np.newaxis,:]
            rest_idx = np.r_[np.arange(i), np.arange(i + 1, j),
                             np.arange(j + 1, len(E_k))]
            rest = np.take(E_k, rest_idx, axis=0)
            X += [np.r_[first, second, rest]]
            idxs += [(i, j)]
    return idxs, np.array(X)

def reconstruct_adjacency(N, idxs, Y_hat):
    """
    Reconstruct the adjacency matrix from a set of indices and predictions.
    """
    A = np.zeros((N, N))
    for (i, j), y in zip(idxs, Y_hat):
        A[i,j] = y
        A[j,i] = y
    return A

def plot_graphs(X, A):
    plt.figure(figsize=(8, 3))
    for idx in range(len(A)):
        plt.subplot(1, len(A), idx + 1)
        feature_norms = np.linalg.norm(X[idx], axis = 1)
        graph = A[idx][(feature_norms > 0),:][:,(feature_norms > 0)]
        G = nx.from_numpy_matrix(graph)
        nx.draw(G, node_color = "black", node_size = 50)
    plt.show()



if __name__ == "__main__":

    argparser = ArgumentParser()
    argparser.add_argument("--N", default=200, type=int)
    argparser.add_argument("--K", default=10, type=int)
    argparser.add_argument("--iterations", default=500, type=int)
    argparser.add_argument("--train", action="store_true")
    args = argparser.parse_args()

    logging.basicConfig(level=logging.DEBUG)
    logger = logging.getLogger(__name__)

    sizes = np.random.choice(np.arange(18, 19), size=args.N)
    X, A = gen_graphs(sizes)
    E = np.array([compute_fgsd_embeddings(a) for a in A])
    E = E[:, :, :args.K]
    E = 0.01 * np.random.randn(*E.shape)

    _, X, Y = convert_pairwise(A, E)
    X = torch.tensor(X, dtype=torch.float)
    Y = torch.tensor(Y, dtype=torch.float)

    edge_predictor = EdgePredictor(args.K)
    optimizer = optim.Adam(edge_predictor.parameters(), lr=0.01)
    for i in range(500):
        optimizer.zero_grad()
        batch_idx = np.random.choice(len(X), size=2000, replace=True)
        loss = edge_predictor.loss(X[batch_idx], Y[batch_idx]).mean()
        loss.backward()
        optimizer.step()
        if i % 1 == 0:
            logger.info(f"Iter: {i}\t" +
                        f"Loss: {loss.mean().data:.2f}\t")

    X, A = gen_graphs([18])
    E = np.array([compute_fgsd_embeddings(a) for a in A])
    E = E[:, :, :args.K]

    idxs, X, Y = convert_pairwise(A, E)
    X = torch.tensor(X, dtype=torch.float)
    Y_hat = torch.sigmoid(edge_predictor.forward(X))

    A_hat = reconstruct_adjacency(18, idxs, Y_hat)
    A_sample = (np.random.rand(*A_hat.shape) < A_hat).astype(int)
    nx.draw(nx.from_numpy_array(A_sample))
    plt.show()

    torch.save(edge_predictor.state_dict(), "./ckpts/edge_predictor.torch")

    edge_predictor = EdgePredictor(args.K)
    edge_predictor.load_state_dict(torch.load("./ckpts/edge_predictor.torch"))


    X, A = gen_graphs(sizes)
    E = np.array([compute_fgsd_embeddings(a) for a in A])
    E = E[:, :, :args.K]

    E = torch.tensor(E, dtype=torch.float)

    model = GF(n_nodes = 18, embedding_dim = 10, num_flows = 1)
    optimizer = optim.Adam(model.parameters(), lr=0.01)

    for i in range(args.iterations):
        optimizer.zero_grad()
        Z, prior_logprob, log_det = model.forward(E)
        loss = -torch.mean(prior_logprob + log_det)
        loss.backward()
        optimizer.step()
        if i % 1 == 0:
            logger.info(f"Iter: {i}\t" +
                        f"Logprob: {(prior_logprob.mean() + log_det.mean()).data:.2f}\t" +
                        f"Prior: {prior_logprob.mean().data:.2f}\t" +
                        f"LogDet: {log_det.mean().data:.2f}")

    breakpoint()

    Z = torch.randn(1, 18, 10)
    E = model.backward(Z)

    plt.imshow(E[0].data.numpy())
    plt.show()

    idxs, X = construct_pairwise_X(E.data.numpy())
    X = torch.tensor(X, dtype=torch.float)
    Y_hat = torch.sigmoid(edge_predictor.forward(X))
    A_hat = reconstruct_adjacency(18, idxs, Y_hat)

    A_sample = (np.random.rand(*A_hat.shape) < A_hat).astype(int)
    nx.draw(nx.from_numpy_array(A_sample))
    plt.show()
