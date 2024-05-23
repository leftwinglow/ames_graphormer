import pandas as pd
import torch
from torch_geometric.data import InMemoryDataset
from torch_geometric.utils import from_smiles

from graphormer.functional import shortest_path_distance
from rdkit import Chem
from tqdm import tqdm


def check_smiles_and_label(smiles, label):
    if torch.isnan(label):
        return f"WARN: No label for {smiles}, skipped"

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return f"WARN: Invalid SMILES {smiles}, skipped"

    return None


class HonmaDataset(InMemoryDataset):
    def __init__(self, root, transform=None, pre_transform=None, max_distance: int = 5):
        self.max_distance = max_distance
        super().__init__(root, transform, pre_transform)
        self.data, self.slices = torch.load(self.processed_paths[0])

    @property
    def raw_file_names(self):
        return ["Honma_New.xlsx"]

    @property
    def processed_file_names(self):
        return ["honma.pt"]

    def process(self):
        """
        Process the raw data and save the processed data.

        This method cleans the raw data, converts it into a format suitable for training,
        and saves the processed data to a .pt file.

        Returns:
            None
        """
        honma = pd.read_excel(self.raw_paths[0])

        data_list = []
        warnings = []

        for smiles, ames in tqdm(
            zip(honma["smiles"], honma["ames"]), total=len(honma), desc="Processing dataset", unit="SMILES"
        ):
            label = torch.tensor([ames], dtype=torch.float)

            warning = check_smiles_and_label(smiles, label)
            if warning:
                warnings.append(warning)
                continue

            data = process(smiles, label, self.max_distance)
            data_list.append(data)

        torch.save(self.collate(data_list), self.processed_paths[0])

        # Print all warnings at the end
        for warning in warnings:
            print(warning)


class HansenDataset(InMemoryDataset):
    def __init__(self, root, transform=None, pre_transform=None, max_distance: int = 5):
        self.max_distance = max_distance
        super().__init__(root, transform, pre_transform)
        self.data, self.slices = torch.load(self.processed_paths[0])

    @property
    def raw_file_names(self):
        return ["Hansen_New.csv"]

    @property
    def processed_file_names(self):
        return ["hansen.pt"]

    def process(self):
        """
        Process the raw data and save the processed data.

        This method cleans the raw data, converts it into a format suitable for training,
        and saves the processed data to a .pt file.

        Returns:
            None
        """
        honma = pd.read_csv(self.raw_paths[0])

        data_list = []
        warnings = []

        for smiles, ames in tqdm(
            zip(honma["smiles"], honma["ames"]), total=len(honma), desc="Processing Hansen Dataset"
        ):
            label = torch.tensor([ames], dtype=torch.float)

            warning = check_smiles_and_label(smiles, label)
            if warning:
                warnings.append(warning)
                continue

            data = process(smiles, label, self.max_distance)
            data_list.append(data)

        torch.save(self.collate(data_list), self.processed_paths[0])

        # Print all warnings at the end
        for warning in warnings:
            print(warning)


class CombinedDataset(InMemoryDataset):
    def __init__(self, root, transform=None, pre_transform=None, max_distance: int = 5):
        self.max_distance = max_distance
        super().__init__(root, transform, pre_transform)
        self.data, self.slices = torch.load(self.processed_paths[0])

    @property
    def raw_file_names(self):
        return ["Combined.xlsx"]

    @property
    def processed_file_names(self):
        return ["combined.pt"]

    def process(self):
        """
        Process the raw data and save the processed data.

        This method cleans the raw data, converts it into a format suitable for training,
        and saves the processed data to a .pt file.

        Returns:
            None
        """
        honma = pd.read_excel(self.raw_paths[0])

        data_list = []
        warnings = []

        for smiles, ames in tqdm(
            zip(honma["smiles"], honma["ames"]), total=len(honma), desc="Processing Combined Dataset"
        ):
            label = torch.tensor([ames], dtype=torch.float)

            warning = check_smiles_and_label(smiles, label)
            if warning:
                warnings.append(warning)
                continue

            data = process(smiles, label, self.max_distance)
            data_list.append(data)

        torch.save(self.collate(data_list), self.processed_paths[0])

        # Print all warnings at the end
        for warning in warnings:
            print(warning)


def process(smiles, label, max_distance):
    data = from_smiles(smiles)
    data.y = label
    node_paths, edge_paths, extra_edge_idxs = shortest_path_distance(data.edge_index, max_distance)

    data.x = torch.cat((torch.ones(1, data.x.shape[1]) * -1, data.x), dim=0)
    new_idxs = torch.stack((torch.zeros(data.x.shape[0]), torch.arange(0, data.x.shape[0])), dim=0).transpose(0, 1)

    data.edge_index = torch.cat((new_idxs, data.edge_index.transpose(0, 1)), dim=0).transpose(0, 1)
    data.node_paths = node_paths
    data.edge_paths = edge_paths
    data.edge_attr = torch.cat(
        (
            torch.ones(1, data.edge_attr.shape[1]) * -1,
            data.edge_attr,
            torch.ones(extra_edge_idxs.shape[0], data.edge_attr.shape[1]) * -1,
        ),
        dim=0,
    )

    assert data.edge_attr.shape[0] - 1 == torch.max(
        data.edge_paths
    ), f"Missing edge attrs for graph!  edge_attr.shape: {data.edge_attr.shape}, max_edge_index: {torch.max(data.edge_paths)}"
    return data


if __name__ == "__main__":
    dataset = HonmaDataset("data")
    dataset = HansenDataset("data")
    dataset = CombinedDataset("data")
    print("Datasets built")
