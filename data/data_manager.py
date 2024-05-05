import pandas as pd
from pathlib import Path
from enum import Enum
import re

class DataReaderSingleMachine():
    def __init__(self, machine_name: str, directory_path: str | Path, csv_file_name: str= "attributes_00.csv"):
        self.machine_name = machine_name
        self.directory_path = Path(directory_path)
        self.data_path = self.directory_path.parent
        self.csv_file_name = csv_file_name
        self.attribute = self.read_attribute()



    def read_attribute(self) -> pd.DataFrame:
        csv_file_path = self.directory_path / self.csv_file_name
        if not csv_file_path.exists():
            return None
        return pd.read_csv(csv_file_path)        

    def get_files_path(self, stage: str):
        if stage not in ["train", "test"]:
            raise ValueError("Stage must be either 'train' or 'test'")
        return list((self.directory_path / stage).glob('*.wav'))       

    def extract_attributes_unique_values(self, stage: str='train'):
        paths = self.get_files_path(stage)
        attributes = []
        for path in paths:
            attributes.append(path_to_dict(path))
        attributes = pd.DataFrame(attributes)
        # remove normal and source columns
        attributes = attributes.drop(columns=["normal", "source"], errors="ignore")
        # get the unique values of each column and return a dict 
        return {col: attributes[col].unique() for col in attributes.columns}
    
    def __repr__(self):
        train_attributes = self.possible_attributes("train")
        return f"Machine: {self.machine_name}, Possible Attributes: {train_attributes}"

def fix_attribute_name(attribute_name: str):
    if attribute_name == "target":
        return "source"
    return attribute_name

def path_to_dict(path: Path): 
    # get the name of the file without the extension
    name = path.stem
    # split the name by '_'
    parts = name.split('_')
    attributes = {}
    for i in range(0, len(parts), 2):
        # if the part is an attribute
        if parts[i].isalpha():
            try:
                attributes[fix_attribute_name(parts[i])] = parts[i+1]
            except:
                pass
    # return the dictionary 
    return attributes

class BaseMachineType(Enum):
    def __init__(self, name, path):
        self._name = name
        self._path = path
        self.data_reader = DataReaderSingleMachine(name, path)

    @property
    def name(self):
        return self._name

    @property
    def path(self):
        return self._path

    @property 
    def reader(self):
        return self.data_reader
    
def get_machines_names(directory_path: str | Path):
    directory_path = Path(directory_path)
    return [(x.name, str(x)) for x in directory_path.iterdir() if x.is_dir()]

def get_machine_manager(directory_path: str | Path):
    machines_name_path = get_machines_names(directory_path)
    DynamicMachineType = Enum('DynamicMachineType', 
                              {name.upper(): (name, path) for name, path in machines_name_path},
                              type=BaseMachineType)
    return DynamicMachineType