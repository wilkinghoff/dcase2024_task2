import pandas as pd
from pathlib import Path
from enum import Enum

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
            self.attribute = None
            return None
        return pd.read_csv(csv_file_path)
        

    def get_files_path(self,stage: str):
        if stage not in ["train", "test"]:
            raise ValueError("Stage must be either train or test")
        return self.directory_path / stage

    def get_files_path_attribute(self, stage: str):
        if self.attribute is None:
            print("No attribute file found for this machine")
            return self.get_files_path(stage)
        paths = self.get_files_path(stage)
        file_attributes = self.attribute[self.attribute['file_name'].str.contains(f"/{stage}/")]
        # file_attribute are relatibe file paths, we need to join them with the full path that is in the list of paths 
        file_attributes.loc[:,'file_name']= file_attributes['file_name'].apply(lambda x: self.data_path / x)
        return file_attributes
    
    def get_possible_parameter(self, stage: str):
        if self.attribute is None:
            print("No attribute file found for this machine")
            return None
        values_col = [col for col in self.attribute.columns if 'v' in col]
        param_col = [col for col in self.attribute.columns if 'p' in col]
        # get the unique values for each parameter column
        unique_param = self.attribute[param_col].apply(lambda x: x.unique())
        unique_values = self.attribute[values_col].apply(lambda x: x.unique())
        return unique_param, unique_values

    def process_files(self, stage: str):
        if self.attribute is None:
            print("No attribute file found for this machine")
            return None
        df  = self.get_files_path_attribute(stage)
        data_path_and_attributes = []
        for index, row in df.iterrows():
            path = row['file_name']
            if not Path(path).exists():
                break
            attribute = row.drop('file_name').to_dict()
            attribute = [v for k, v in attribute.items() if 'v' in k]
            data_path_and_attributes.append((path, attribute))
        return data_path_and_attributes

    def __repr__(self):
        return f"""Machine: {self.machine_name}, attribute parameterfile: \n {pd.DataFrame(self.get_possible_parameter('train')[0])} 
        \n {pd.DataFrame(self.get_possible_parameter('train')[1])}"""
    


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