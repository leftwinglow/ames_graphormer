from typing import Tuple, Dict, List
from multiprocessing import Pool

import networkx as nx
from torch_geometric.data import Data
from torch_geometric.utils.convert import to_networkx


def floyd_warshall_source_to_all(G, source, cutoff=None):
    # хочу typing
    if source not in G:
        raise nx.NodeNotFound("Source {} not in G".format(source))

    edges = {edge: i for i, edge in enumerate(G.edges())}

    level = 0  # the current level
    nextlevel = [source]  # list of nodes to check at next level
    node_paths = {source: [source]}  # paths dictionary (paths to key from source)
    edge_paths = {source: []}

    while nextlevel:
        thislevel = nextlevel
        nextlevel = []
        for v in thislevel:
            for w in G[v]:
                if w not in node_paths:
                    node_paths[w] = node_paths[v] + [w]
                    edge_paths[w] = edge_paths[v] + [edges[tuple(node_paths[w][-2:])]]
                    nextlevel.append(w)

        level = level + 1

        if cutoff is not None and cutoff <= level:
            break

    return node_paths, edge_paths


def all_pairs_shortest_path(G) -> Tuple[Dict[int, List[int]], Dict[int, List[int]]]:
    paths = {n: floyd_warshall_source_to_all(G, n) for n in G}
    node_paths = {n: paths[n][0] for n in paths}
    edge_paths = {n: paths[n][1] for n in paths}
    return node_paths, edge_paths


def shortest_path_distance(data: Data) -> Tuple[Dict[int, List[int]], Dict[int, List[int]]]:
    G = to_networkx(data)
    node_paths, edge_paths = all_pairs_shortest_path(G)
    return node_paths, edge_paths


def batched_all_pairs_shortest_path(G) -> Tuple[Dict[int, List[int]], Dict[int, List[int]]]:
    # same code as `all_pairs_shortest_path`?
    paths = {n: floyd_warshall_source_to_all(G, n) for n in G}
    node_paths = {n: paths[n][0] for n in paths}
    edge_paths = {n: paths[n][1] for n in paths}
    return node_paths, edge_paths


def batched_shortest_path_distance(data) -> Tuple[Dict[int, List[int]], Dict[int, List[int]]]:
    # type(Data) == Batch?
    pool = Pool()
    relabeled_graphs = []
    shift = 0
    for graph in pool.map(to_networkx, data.to_data_list()):
        num_nodes = graph.number_of_nodes()
        relabeled_graphs.append(nx.relabel_nodes(graph, {i: i + shift for i in range(num_nodes)}))
        shift += num_nodes

    node_paths = {}
    edge_paths = {}

    for path in pool.map(batched_all_pairs_shortest_path, relabeled_graphs):
        for k, v in path[0].items():
            node_paths[k] = v
        for k, v in path[1].items():
            edge_paths[k] = v

    return node_paths, edge_paths
