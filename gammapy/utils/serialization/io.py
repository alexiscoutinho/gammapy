# Licensed under a 3-clause BSD style license - see LICENSE.rst
"""Utilities to serialize models."""
import astropy.units as u
from gammapy.image import models as spatial
from gammapy.spectrum import models as spectral
from gammapy.cube.models import (
    SkyModel,
    SkyModels,
    SkyDiffuseCube,
    BackgroundModel,
    BackgroundModels,
)
from gammapy.utils.fitting import Parameters

__all__ = ["models_to_dict", "dict_to_models", "models_to_datasets", "datasets_to_dict"]


def models_to_dict(models, selection="all"):
    """Convert list of models to dict.

    Parameters
    -----------
    models : list
        Python list of Model objects
    selection : {"all", "simple"}
        Selection of information to include
    """
    models_data = []
    for model in models:
        model_data = _model_to_dict(model, selection)

        # De-duplicate if model appears several times
        if model_data not in models_data:
            models_data.append(model_data)

    return {"components": models_data}


def _model_to_dict(model, selection):
    data = {}
    data["name"] = getattr(model, "name", model.__class__.__name__)
    try:
        data["id"] = model.dataset_id
    except AttributeError:
        pass
    if getattr(model, "filename", None) is not None:
        data["filename"] = model.filename
    if model.__class__.__name__ == "SkyModel":
        data["spatial"] = model.spatial_model.to_dict(selection)
        if getattr(model.spatial_model, "filename", None) is not None:
            data["spatial"]["filename"] = model.spatial_model.filename
        data["spectral"] = model.spectral_model.to_dict(selection)
    else:
        data["model"] = model.to_dict(selection)

    return data


def dict_to_models(data):
    """De-serialise model data to Model objects.

    Parameters
    -----------
    data : dict
        Serialised model information
    """
    models = []
    for model in data["components"]:
        if "model" in model:
            if model["model"]["type"] == "BackgroundModel":
                continue
            else:
                raise NotImplementedError

        model = _dict_to_skymodel(model)
        models.append(model)

    return models


def _dict_to_skymodel(model):
    item = model["spatial"]
    if "filename" in item:
        spatial_model = getattr(spatial, item["type"]).read(item["filename"])
        spatial_model.filename = item["filename"]
        spatial_model.parameters = Parameters.from_dict(item)
    else:
        params = {x["name"]: x["value"] * u.Unit(x["unit"]) for x in item["parameters"]}
        spatial_model = getattr(spatial, item["type"])(**params)
        spatial_model.parameters = Parameters.from_dict(item)

    item = model["spectral"]
    if "energy" in item:
        energy = u.Quantity(item["energy"]["data"], item["energy"]["unit"])
        values = u.Quantity(item["values"]["data"], item["values"]["unit"])
        params = {"energy": energy, "values": values}
        spectral_model = getattr(spectral, item["type"])(**params)
        spectral_model.parameters = Parameters.from_dict(item)
    else:
        params = {x["name"]: x["value"] * u.Unit(x["unit"]) for x in item["parameters"]}
        spectral_model = getattr(spectral, item["type"])(**params)
        spectral_model.parameters = Parameters.from_dict(item)

    return SkyModel(
        name=model["name"], spatial_model=spatial_model, spectral_model=spectral_model
    )


def datasets_to_dict(datasets, path, selection, overwrite):
    from ..serialization import models_to_dict
    from ...cube.models import BackgroundModels, SkyModels

    models_list = []
    backgrounds_list = []
    datasets_dictlist = []
    for dataset in datasets:
        filename = path + "maps_" + dataset.dataset_id + ".fits"
        dataset.write(filename, overwrite)
        datasets_dictlist.append({"id": dataset.dataset_id, "filename": filename})

        if isinstance(dataset.background_model, BackgroundModels):
            backgrounds = dataset.background_model.models
        else:
            backgrounds = [dataset.background_model]
        if isinstance(dataset.model, SkyModels):
            models = dataset.model.skymodels
        else:
            models = [dataset.model]
        # TODO: remove isinstance checks once #2102  is resolved

        models_list += models
        backgrounds_list += backgrounds

    datasets_dict = {"datasets": datasets_dictlist}
    components_dict = models_to_dict(models_list + backgrounds_list, selection)
    return datasets_dict, components_dict


class models_to_datasets:
    """add models and backgrounds to datasets
    
    Parameters
    ----------
    datasets : `~gammapy.utils.fitting.Datasets`
        Datasets
    components : dict
        dict describing model components
    get_lists : bool
        get the datasets, models and backgrounds lists separetely (used to initialize FitManager)
        
    """

    def __init__(self, datasets, components):

        self.datasets = datasets.datasets
        self.models = dict_to_models(components)
        self.backgrounds_local = []
        self.backgrounds_global = []

        self.params_register = {}
        self.cube_register = {}

        for dataset in self.datasets:
            self.update_dataset(dataset, components)

    def update_dataset(self, dataset, components):
        if not isinstance(dataset.background_model, BackgroundModels):
            dataset.background_model = BackgroundModels([dataset.background_model])
        # TODO: remove isinstance checks once #2102  is resolved
        BKG_names = [model.name for model in dataset.background_model.models]
        backgrounds = []
        for component in components["components"]:
            if (
                "model" in component
                and component["model"]["type"] == "BackgroundModel"
                and component["id"] in ["global", "local", dataset.dataset_id]
            ):
                background_model = self.add_background(dataset, component, BKG_names)
                self.link_parameters(dataset, component, background_model)
                backgrounds.append(background_model)
        dataset.background_model = BackgroundModels(backgrounds)
        dataset.model = SkyModels(self.models)

    def add_background(self, dataset, component, BKG_names):
        if "filename" in component:
            # check if file is already loaded in memory else read
            try:
                cube = self.cube_register[component["name"]]
            except KeyError:
                cube = SkyDiffuseCube.read(component["filename"])
                self.cube_register[component["name"]] = cube
            background_model = BackgroundModel.from_skymodel(
                cube, exposure=dataset.exposure, psf=dataset.psf, edisp=dataset.edisp
            )
        else:
            if component["name"].strip().upper() in BKG_names:
                BGind = BKG_names.index(component["name"].strip().upper())
            elif component["name"] in BKG_names:
                BGind = BKG_names.index(component["name"])
            else:
                raise ValueError("Unknown Background")
            background_model = dataset.background_model.models[BGind]
        background_model.name = component["name"]
        return background_model

    def link_parameters(self, dataset, component, background_model):
        """ link parameters to background """
        if component["id"] == "global":
            try:
                params = self.params_register[component["name"]]
            except KeyError:
                params = Parameters.from_dict(component["model"])
                self.params_register[component["name"]] = params
            background_model.parameters = params
            background_model.dataset_id = "global"
            self.backgrounds_global.append(background_model)
        elif component["id"] in ["local", dataset.dataset_id]:
            background_model.parameters = Parameters.from_dict(component["model"])
            background_model.dataset_id = dataset.dataset_id
            self.backgrounds_local.append(background_model)
