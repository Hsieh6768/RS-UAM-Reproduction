import numpy as np


METERS_PER_MILE = 1609.344
EARTH_RADIUS_M = 6371009.0


def mph_to_mps(mph):
    return mph * METERS_PER_MILE / 3600


def sphere_xyz(lon_lat):
    lon, lat = np.radians(np.asarray(lon_lat)).T
    return np.column_stack((np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)))


def distance_m(origin, destination):
    chord = np.linalg.norm(sphere_xyz(origin) - sphere_xyz(destination), axis=1)
    return chord_distance_m(chord)


def chord_distance_m(chord):
    return 2 * EARTH_RADIUS_M * np.arcsin(np.minimum(chord / 2, 1.0))
