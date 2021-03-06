#!/usr/bin/env python
# TODO remove all unused utility functions
# TODO figure out why voxel_array sometimes crashes? is it because I didn't have /mnt/ ?
# update 09162020 I think it is an issue with numba
# TODO I will try to fix it by installing numba and removing Bcov dependencies
# TODO better documentation, delta SAP? As implemented, reporting the change in SAP requires stdout capture

# python libraries
from __future__ import division
__author__ = "Brian Coventry, Tim Huddy, Philip Leung"
__copyright__ = None
__credits__ = ["Brian Coventry", "Tim Huddy","Philip Leung",
        "Rosettacommons"]
__license__ = "MIT"
__version__ = "0.8.0"
__maintainer__ = "Philip Leung"
__email__ = "pleung@cs.washington.edu"
__status__ = "Prototype"
import argparse
from collections import defaultdict
import itertools
import math
import os
import subprocess
import sys
import time
# external libraries
import numpy as np
from numba import njit
# pyrosetta libraries
from pyrosetta import *
from pyrosetta.rosetta import *
from pyrosetta.rosetta.core.io.silent import (SilentFileData, 
        SilentFileOptions)
from pyrosetta.rosetta.core.chemical import aa_from_oneletter_code
from pyrosetta.rosetta.core.pose import Pose, pose_residue_is_terminal
from pyrosetta.rosetta.protocols.simple_moves import (MutateResidue, 
                                                      SimpleThreadingMover)
from pyrosetta.rosetta.core.select import get_residues_from_subset
from pyrosetta.rosetta.core.select import residue_selector
from pyrosetta.rosetta.core.select.residue_selector import (
    AndResidueSelector, NeighborhoodResidueSelector, NotResidueSelector,
    OrResidueSelector, PrimarySequenceNeighborhoodSelector,
    ResidueIndexSelector, ResidueNameSelector)
from pyrosetta.rosetta.core.scoring import ScoreFunction, ScoreType
from pyrosetta.rosetta.core.scoring.methods import EnergyMethodOptions
from pyrosetta.rosetta.core.pack.task import operation
from pyrosetta.rosetta.protocols.task_operations import (LinkResidues,
    LimitAromaChi2Operation, PruneBuriedUnsatsOperation)
from pyrosetta.rosetta.protocols.aa_composition import (
    AddCompositionConstraintMover)
from pyrosetta.rosetta.protocols.denovo_design.movers import FastDesign
from pyrosetta.rosetta.protocols.protein_interface_design import (
    FavorNativeResidue)
from pyrosetta.rosetta.protocols.relax import FastRelax
from pyrosetta.rosetta.protocols.rosetta_scripts import XmlObjects
flags = """
-corrections::beta_nov16
-holes:dalphaball 
/home/bcov/dev_rosetta/main/source/external/DAlpahBall/DAlphaBall.gcc
-ignore_unrecognized_res 1
-in:file:silent_struct_type binary 
-keep_input_scores false
-mute core.select.residue_selector.SecondaryStructureSelector
-mute core.select.residue_selector.PrimarySequenceNeighborhoodSelector
-mute protocols.DsspMover
-mute core.chemical
-mute core.conformation
-mute core.pack
-mute core.scoring
"""
pyrosetta.init(' '.join(flags.replace('\n\t', ' ').split()))
# positional arguments
parser = argparse.ArgumentParser(
        description="Use to redesign based on per-residue score in b-factor.")
ingroup = parser.add_argument_group("input filename(s)")
inputs = ingroup.add_mutually_exclusive_group(required=True)
inputs.add_argument("--in:file:silent", type=str)#, default='')
inputs.add_argument("--pdbs", type=str, nargs='*')
# required arguments
group = parser.add_mutually_exclusive_group(required=True)
group.add_argument("--redesign_above", type=float,
        help="any residue above this score will be redesigned")
group.add_argument("--redesign_below", type=float,
        help="any residue below this score will be redesigned")
# optional arguments
parser.add_argument("--zero_adjust", type=float, default=0)
parser.add_argument("--worst_n", type=int, default=25,
        help="the worst n residues that don't meet the cutoff to redesign")
parser.add_argument("--radius", type=int, default=5,
        help="the radius size for the voxel grid")
parser.add_argument("--flexbb", dest='flexbb', action='store_true',
        help="turns on flexible bb design")
parser.add_argument("--use_sasa", dest='use_sasa', action='store_true',
        help="use SASA to designate layers instead of # neighbors")
parser.add_argument("--cutoffs", type=float, nargs='*', default=[5.2,2.0],
        help="layer definition cutoffs for SASA or # neighbors")
parser.add_argument("--lock_resis", type=int, nargs='*', default=[],
        help="list of residue indices not to design, ex 1 7 9 11")
parser.add_argument("--relax_script", type=str, default='MonomerDesign2019',
        help="the name of the relax script to use")
parser.add_argument("--up_ele", dest='up_ele', action='store_true',
        help="turns on upweighting of electrostatic interactions")
parser.add_argument("--no_prescore", dest='prescore', action='store_false',
        help="skip calculating SAP score, useful if file is already scored")
parser.add_argument("--no_rescore", dest='rescore', action='store_false',
        help="skip recalculating SAP score, use if you're not doing SAP")
parser.add_argument("--chunk", dest='chunk', action='store_true',
        help="design 10 residues at a time, fast but less optimal results")
parser.add_argument("--lock_HNQST", dest='lock_HNQST', action='store_true',
        help="lock HIS, ASN, GLN, SER, and THR, to avoid breaking HBNets")
parser.add_argument("--lock_PG", dest='lock_PG', action='store_true',
        help="lock PRO and GLY, probably a good idea")
parser.add_argument("--lock_YW", dest='lock_YW', action='store_true',
        help="lock TYR and TRP, those are often present for a good reason")
parser.add_argument("--penalize_ARG", dest='penalize_ARG', 
        action='store_true',
        help="add a small penalty to ARG usage, perhaps improving solubility")
parser.add_argument("--encourage_mutation", dest='encourage_mutation',
        action='store_true',
        help="add a composition restraint to bias the design")
parser.add_argument("--restraint_weight", type=float, default=-1.0,
        help="negative incentivizes mutation, positive favors the native seq")

@njit(fastmath=True,cache=False)
def numba_do_surface_crawl(start, normal, direction, distance, arr, lb, ub, cs, shape):

    up_down_steps = 20
    up_down_step = cs[0]*0.3
    normal_step = normal*up_down_step

    forward_step_size = cs[0] 
    forward_step = forward_step_size * direction

    fail = np.array([0], np.bool_)

    traversed = []
    traveled = 0

    prev = start
    current = start
    while ( traveled < distance ):

        surf = numba_seek_to_surface(current, normal_step, up_down_steps, fail, arr, lb, ub, cs, shape)
        if ( fail[0] ):
            return traversed, traveled

        traversed.append(surf)

        # traveled += distance_two_pts( surf, prev )
        traveled = distance_two_pts( surf, start )
        prev = surf
        current = prev + forward_step

    return traversed, traveled

@njit(fastmath=True,cache=False)
def numba_add_to_near_grid(pts, store_vals, atom_radius, near_grid, dist_grid, lb, ub, cs, shape):
    for i in range(len(pts)):
        pt = pts[i]
        store_val = store_vals[i]
        numba_store_near_grid(near_grid, dist_grid, atom_radius*2, pt, store_val, lb, ub, cs, shape)


@njit(fastmath=True,cache=False)
def numba_store_near_grid(near_grid, dist_grid, _x, pt, idx, lb, ub, cs, shape):

    # these should like really be here
    assert(len(pt) == 3)

    low_high = np.array([[0, 0, 0], [0, 0, 0]], dtype=np.float_)
    for i in range(3):
        low_high[0, i] = pt[i] - _x
        low_high[1, i] = pt[i] + _x

    for i in range(3):
        assert( low_high[0, i] > lb[i] + cs[i] )
        assert( low_high[1, i] < ub[i] - cs[i] )

    # transform bounds into upper and lower corners in voxel array indices
    bounds = xform_vectors( low_high, lb, cs, shape )

    # translate voxel array indices back to 3d coords and do distance check
    _x2 = _x*_x
     
    for i in range(bounds[0, 0], bounds[1, 0] + 1):
        x = numba_ind_index_to_center(i, lb[0], cs[0]) - pt[0]
        x2 = x*x
        for j in range(bounds[0, 1], bounds[1, 1] + 1):
            y = numba_ind_index_to_center(j, lb[1], cs[1]) - pt[1]
            y2 = y*y
            for k in range(bounds[0, 2], bounds[1, 2] + 1):
                z = numba_ind_index_to_center(k, lb[2], cs[2]) - pt[2]
                z2 = z*z
                dist2 = x2 + y2 + z2
                if ( dist2 < _x2 ):
                    if ( dist2 < dist_grid[i, j, k] ):
                        near_grid[i, j, k] = idx
                        dist_grid[i, j, k] = dist2

@njit(fastmath=True,cache=False)
def numba_make_sum_grid(pts, atom_radius, arr, lb, ub, cs, shape, store_val):
    for i in range(len(pts)):
        pt = pts[i]
        numba_indices_add_within_x_of(arr, store_val, atom_radius*2, pt, lb, ub, cs, shape)


@njit(fastmath=True,cache=False)
def numba_indices_add_within_x_of(arr, to_store, _x, pt, lb, ub, cs, shape):

    # these should like really be here
    assert(len(pt) == 3)

    low_high = np.array([[0, 0, 0], [0, 0, 0]], dtype=np.float_)
    for i in range(3):
        low_high[0, i] = pt[i] - _x
        low_high[1, i] = pt[i] + _x

    for i in range(3):
        assert( low_high[0, i] > lb[i] + cs[i] )
        assert( low_high[1, i] < ub[i] - cs[i] )

    # transform bounds into upper and lower corners in voxel array indices
    bounds = xform_vectors( low_high, lb, cs, shape )


    # translate voxel array indices back to 3d coords and do distance check
    _x2 = _x*_x
     
    for i in range(bounds[0, 0], bounds[1, 0] + 1):
        x = numba_ind_index_to_center(i, lb[0], cs[0]) - pt[0]
        x2 = x*x
        for j in range(bounds[0, 1], bounds[1, 1] + 1):
            y = numba_ind_index_to_center(j, lb[1], cs[1]) - pt[1]
            y2 = y*y
            for k in range(bounds[0, 2], bounds[1, 2] + 1):
                z = numba_ind_index_to_center(k, lb[2], cs[2]) - pt[2]
                z2 = z*z
                if ( x2 + y2 + z2 < _x2 ):
                    arr[i, j, k] += to_store

@njit(fastmath=True,cache=False)
def numba_make_clashgrid(pts, atom_radius, arr, lb, ub, cs, shape, store_val):
    for i in range(len(pts)):
        pt = pts[i]
        numba_indices_store_within_x_of(arr, store_val, atom_radius*2, pt, lb, ub, cs, shape)

@njit(fastmath=True,cache=False)
def numba_make_clashgrid_var_atom_radius(pts, atom_radius, arr, lb, ub, cs, shape, store_val):
    for i in range(len(pts)):
        pt = pts[i]
        radius = atom_radius[i]
        numba_indices_store_within_x_of(arr, store_val, radius*2, pt, lb, ub, cs, shape)

@njit(fastmath=True,cache=False)
def numba_indices_store_within_x_of(arr, to_store, _x, pt, lb, ub, cs, shape):

    # these should like really be here
    assert(len(pt) == 3)

    low_high = np.array([[0, 0, 0], [0, 0, 0]], dtype=np.float_)
    for i in range(3):
        low_high[0, i] = pt[i] - _x
        low_high[1, i] = pt[i] + _x

    for i in range(3):
        assert( low_high[0, i] > lb[i] + cs[i] )
        assert( low_high[1, i] < ub[i] - cs[i] )

    # transform bounds into upper and lower corners in voxel array indices
    bounds = xform_vectors( low_high, lb, cs, shape )


    # translate voxel array indices back to 3d coords and do distance check
    _x2 = _x*_x
     
    for i in range(bounds[0, 0], bounds[1, 0] + 1):
        x = numba_ind_index_to_center(i, lb[0], cs[0]) - pt[0]
        x2 = x*x
        for j in range(bounds[0, 1], bounds[1, 1] + 1):
            y = numba_ind_index_to_center(j, lb[1], cs[1]) - pt[1]
            y2 = y*y
            for k in range(bounds[0, 2], bounds[1, 2] + 1):
                z = numba_ind_index_to_center(k, lb[2], cs[2]) - pt[2]
                z2 = z*z
                if ( x2 + y2 + z2 < _x2 ):
                    arr[i, j, k] = to_store

@njit(fastmath=True,cache=False)
def numba_index_to_center(vec, lb, cs, shape):
    out = np.array([0, 0, 0])
    for i in range(3):
        out = (vec[i] + 0.5) * cs[i] + lb[i]
    return out

@njit(fastmath=True,cache=False)
def numba_ind_index_to_center(i, lb, cs):
    return (i + 0.5) * cs + lb

@njit(fastmath=True,cache=False)
def numba_indices_to_centers(inds, lb, cs):
    out = np.zeros((len(inds), len(lb)), dtype=np.float_)
    for i in range(len(inds)):
        for j in range(len(lb)):
            out[i, j] = (inds[i, j] + 0.5) * cs[j] + lb[j]
    return out

@njit(fastmath=True,cache=False)
def xform_vectors(vecs, lb, cs, shape):
    out = np.zeros((len(vecs), len(lb)), dtype=np.int_)
    return xform_vectors_w_out(vecs, lb, cs, shape, out)

@njit(fastmath=True,cache=False)
def xform_vectors_w_out(vecs, lb, cs, shape, out):
    for i in range(len(vecs)):
        for j in range(len(lb)):
            out[i, j] = xform_1_pt(vecs[i, j], lb[j], cs[j], shape[j])
    return out

@njit(fastmath=True,cache=False)
def xform_vector(vec, lb, cs, shape):
    out = np.array([0, 0, 0], dtype=np.int_)
    for i in range(len(vec)):
        out[i] = xform_1_pt(vec[i], lb[i], cs[i], shape[i])
    return out

@njit(fastmath=True,cache=False)
def xform_1_pt(pt, lb, cs, shape):
    x = np.int( ( pt - lb ) / cs )
    if ( x <= 0 ):
        return np.int(0)
    if ( x >= shape-1 ):
        return shape-1
    return x

@njit(fastmath=True,cache=False)
def lookup_vec(vec, arr, lb, cs, shape):
    return arr[xform_1_pt(vec[0], lb[0], cs[0], shape[0]),
               xform_1_pt(vec[1], lb[1], cs[1], shape[1]),
               xform_1_pt(vec[2], lb[2], cs[2], shape[2])
            ]

@njit(fastmath=True,cache=False)
def numba_clash_check(pts, max_clashes, arr, lb, cs):
    
    clashes = 0

    for i in range(len(pts)):
        pt = pts[i]
        x = xform_1_pt(pt[0], lb[0], cs[0], arr.shape[0])
        y = xform_1_pt(pt[1], lb[1], cs[1], arr.shape[1])
        z = xform_1_pt(pt[2], lb[2], cs[2], arr.shape[2])

        clashes += arr[x, y, z]

        if ( clashes > max_clashes ):
            return clashes

    return clashes

@njit(fastmath=True,cache=False)
def numba_ray_trace_many(starts, ends, max_clashes, arr, lb, cs, debug=False):
    clashes = np.zeros(len(starts), np.int_)
    for i in range(len(starts)):
        clashes[i] = numba_ray_trace(starts[i], ends[i], max_clashes, arr, lb, cs, debug)

    return clashes

@njit(fastmath=True,cache=False)
def numba_ray_trace(start, end, max_clashes, arr, lb, cs, debug=False):

    arr_start = np.zeros((3), np.float_)
    arr_start[0] = xform_1_pt(start[0], lb[0], cs[0], arr.shape[0])
    arr_start[1] = xform_1_pt(start[1], lb[1], cs[1], arr.shape[1])
    arr_start[2] = xform_1_pt(start[2], lb[2], cs[2], arr.shape[2])

    arr_end = np.zeros((3), np.float_)
    arr_end[0] = xform_1_pt(end[0], lb[0], cs[0], arr.shape[0])
    arr_end[1] = xform_1_pt(end[1], lb[1], cs[1], arr.shape[1])
    arr_end[2] = xform_1_pt(end[2], lb[2], cs[2], arr.shape[2])

    slope = arr_end - arr_start
    largest = np.max(np.abs(slope))
    slope /= largest

    max_iter = largest+1

    clashes = 0
    x = arr_start[0]
    y = arr_start[1]
    z = arr_start[2]
    for i in range(max_iter):
        clashes += arr[int(x+0.5), int(y+0.5), int(z+0.5)]
        if ( debug ):
            print(i, largest, slope)
            arr[int(x+0.5), int(y+0.5), int(z+0.5)] = True
        if ( clashes >= max_clashes ):
            return clashes
        x += slope[0]
        y += slope[1]
        z += slope[2]

    return clashes

@njit(fastmath=True,cache=False)
def _lookup_3d(null_val, loc, arr, shape):
    if ( loc[0] == 0 or loc[0] >= shape[0]-1):
        return null_val
    if ( loc[1] == 0 or loc[1] >= shape[1]-1):
        return null_val
    if ( loc[2] == 0 or loc[2] >= shape[2]-1):
        return null_val
    return arr[loc[0], loc[1], loc[2]]

@njit(fastmath=True,cache=False)
def _increase_ptr( ptr, offset, cur_stack, stacks, stack_sizes, generate_stack ):
    ptr += 1
    if ( ptr == stack_sizes[cur_stack] ):
        offset += stack_sizes[cur_stack]
        cur_stack += 1
        if ( generate_stack ):
            stacks[cur_stack] = np.zeros((stack_sizes[cur_stack], 3), np.int_)
    return ptr, offset, cur_stack

# faster flood fill but harder to write
@njit(fastmath=True,cache=False)
def numba_flood_fill_3d_from_here(fill_val, overwrite_val, start_idx, arr, lb, ub, cs, shape ):

    num_points = shape[0]*shape[1]*shape[2]

    stack_size0 = num_points//100 + 2 # plus 2 so the 2nd element can't be in 2
    stack_size1 = num_points//10 + 1
    stack_size2 = num_points

    stack0 = np.zeros((stack_size0, 3), np.int_)
    stack1 = np.zeros((1, 3), np.int_)
    stack2 = np.zeros((1, 3), np.int_)

    stack_sizes = [stack_size0, stack_size1, stack_size2]
    stacks = [stack0, stack1, stack2]


    stack0[0] = start_idx
    arr[start_idx[0], start_idx[1], start_idx[2]] = fill_val
    stack0[0] = start_idx

    process_ptr = 0
    process_offset = 0
    cur_process_stack = 0

    set_offset = 0
    cur_set_stack = 0
    set_ptr = 1

    while ( process_ptr < set_ptr ):
        # print(set_ptr, stack_size0)

        loc = stacks[cur_process_stack][process_ptr - process_offset]

        process_ptr, process_offset, cur_process_stack = _increase_ptr( 
                            process_ptr, process_offset, cur_process_stack, stacks, stack_sizes, False)

        # right
        loc[0] += 1
        if ( _lookup_3d(fill_val, loc, arr, shape) == overwrite_val ):
            arr[loc[0], loc[1], loc[2]] = fill_val
            stacks[cur_set_stack][set_ptr - set_offset] = loc
            set_ptr, set_offset, cur_set_stack = _increase_ptr( 
                            set_ptr, set_offset, cur_set_stack, stacks, stack_sizes, True)
        # left
        loc[0] -= 2
        if ( _lookup_3d(fill_val, loc, arr, shape) == overwrite_val ):
            arr[loc[0], loc[1], loc[2]] = fill_val
            stacks[cur_set_stack][set_ptr - set_offset] = loc
            set_ptr, set_offset, cur_set_stack = _increase_ptr( 
                            set_ptr, set_offset, cur_set_stack, stacks, stack_sizes, True)
        # down
        loc[0] += 1
        loc[1] += 1
        if ( _lookup_3d(fill_val, loc, arr, shape) == overwrite_val ):
            arr[loc[0], loc[1], loc[2]] = fill_val
            stacks[cur_set_stack][set_ptr - set_offset] = loc
            set_ptr, set_offset, cur_set_stack = _increase_ptr( 
                            set_ptr, set_offset, cur_set_stack, stacks, stack_sizes, True)
        # up
        loc[1] -= 2
        if ( _lookup_3d(fill_val, loc, arr, shape) == overwrite_val ):
            arr[loc[0], loc[1], loc[2]] = fill_val
            stacks[cur_set_stack][set_ptr - set_offset] = loc
            set_ptr, set_offset, cur_set_stack = _increase_ptr( 
                            set_ptr, set_offset, cur_set_stack, stacks, stack_sizes, True)
        # forward
        loc[1] += 1
        loc[2] += 1
        if ( _lookup_3d(fill_val, loc, arr, shape) == overwrite_val ):
            arr[loc[0], loc[1], loc[2]] = fill_val
            stacks[cur_set_stack][set_ptr - set_offset] = loc
            set_ptr, set_offset, cur_set_stack = _increase_ptr( 
                            set_ptr, set_offset, cur_set_stack, stacks, stack_sizes, True)
        # backward
        loc[2] -= 2
        if ( _lookup_3d(fill_val, loc, arr, shape) == overwrite_val ):
            arr[loc[0], loc[1], loc[2]] = fill_val
            stacks[cur_set_stack][set_ptr - set_offset] = loc
            set_ptr, set_offset, cur_set_stack = _increase_ptr( 
                            set_ptr, set_offset, cur_set_stack, stacks, stack_sizes, True)
        loc[2] += 1

# this does forward filling going from 0->hi and hi->0
# is this a fast way to do it? no idea
# don't allow diagonal filling for speed
@njit(fastmath=True,cache=False)
def numba_flood_fill_3d(fill_val, overwrite_val, arr, lb, ub, cs, shape ):

    # for cache-coherence, we always iter on z last
    any_changed = True
    while (any_changed):
        any_changed = False

        # forward fill in positive direction
        for x in range(1, shape[0]-2):
            for y in range(1, shape[1]-2):
                for z in range(1, shape[2]-2):
                    if ( arr[x, y, z] != fill_val ):
                        continue
                    if ( arr[x, y, z+1] == overwrite_val ):
                        arr[x, y, z+1] = fill_val
                        any_changed = True
                    if ( arr[x, y+1, z] == overwrite_val ):
                        arr[x, y+1, z] = fill_val
                        any_changed = True
                    if ( arr[x+1, y, z] == overwrite_val ):
                        arr[x+1, y, z] = fill_val
                        any_changed = True

        # forward fill in negative direction
        for x in range(shape[0]-2, 1, -1):
            for y in range(shape[1]-2, 1, -1):
                for z in range(shape[2]-2, 1, -1):
                    if ( arr[x, y, z] != fill_val ):
                        continue
                    if ( arr[x, y, z-1] == overwrite_val ):
                        arr[x, y, z-1] = fill_val
                        any_changed = True
                    if ( arr[x, y-1, z] == overwrite_val ):
                        arr[x, y-1, z] = fill_val
                        any_changed = True
                    if ( arr[x-1, y, z] == overwrite_val ):
                        arr[x-1, y, z] = fill_val
                        any_changed = True

# this does forward filling going from 0->hi and hi->0
# is this a fast way to do it? no idea
# don't allow diagonal filling for speed
@njit(fastmath=True,cache=False)
def numba_flood_fill_2d(fill_val, overwrite_val, arr, lb, ub, cs, shape  ):

    # for cache-coherence, we always iter on z last
    any_changed = True
    while (any_changed):
        any_changed = False

        # forward fill in positive direction
        for x in range(1, shape[0]-2):
            for y in range(1, shape[1]-2):
                if ( arr[x, y] != fill_val ):
                    continue
                if ( arr[x, y+1] == overwrite_val ):
                    arr[x, y+1] = fill_val
                    any_changed = True
                if ( arr[x+1, y] == overwrite_val ):
                    arr[x+1, y] = fill_val
                    any_changed = True

        # forward fill in negative direction
        for x in range(shape[0]-2, 1, -1):
            for y in range(shape[1]-2, 1, -1):
                if ( arr[x, y] != fill_val ):
                    continue
                if ( arr[x, y-1] == overwrite_val ):
                    arr[x, y-1] = fill_val
                    any_changed = True
                if ( arr[x-1, y] == overwrite_val ):
                    arr[x-1, y] = fill_val
                    any_changed = True

class VoxelArray:

    def __init__(self, lbs, ubs, cbs, dtype="f8", arr=None):

        self.dim = len(lbs)
        self.lb = lbs
        self.ub = ubs
        self.cs = cbs

        if ( arr is None ):
            extents = self.floats_to_indices_no_clip(np.array([self.ub]))[0]
            extents += 1
            self.arr = np.zeros(extents, dtype=dtype)
        else:
            self.arr = arr

    def copy(self):
        vx = VoxelArray(self.lb, self.ub, self.cs, self.arr.dtype, self.arr.copy())
        return vx

    def save(self, fname):
        save_dict = {
            "lb":self.lb,
            "ub":self.ub,
            "cs":self.cs,
            "arr":self.arr
        }
        np.save(fname, save_dict)

    @classmethod
    def load(cls, fname):
        save_dict = np.load(fname, allow_pickle=True).item()
        lb = save_dict["lb"]
        ub = save_dict["ub"]
        cs = save_dict["cs"]
        arr = save_dict["arr"]

        return cls(lb, ub, cs, arr=arr)

    # only used in __init__ 
    def floats_to_indices_no_clip(self, pts):
        inds = np.zeros((len(pts), self.dim), dtype=np.int)
        for i in range(self.dim):
            inds[:,i] = ((pts[:,i] - self.lb[i] ) / self.cs[i])
        return inds

    def floats_to_indices(self, pts, out=None):
        if ( out is None ):
            out = np.zeros((len(pts), self.dim), dtype=np.int)

        return xform_vectors_w_out(pts, self.lb, self.cs, self.arr.shape, out)

    def indices_to_centers(self, inds ):
        return numba_indices_to_centers(inds, self.lb, self.cs)

    def all_indices(self):
        ranges = []
        for i in range(self.dim):
            ranges.append(list(range(self.arr.shape[i])))
        inds = np.array(list(itertools.product(*ranges)))
        return inds

    def all_centers(self):
        inds = self.all_indices()
        return self.indices_to_centers(inds)

    # One would usuallly type assert(voxel.oob_is_zero())
    def oob_is_zero(self):
        # This could certainly be made more efficient
        all_indices = self.all_indices()
        is_good = np.zeros(len(all_indices))
        for i in range(self.dim):
            is_good |= (all_indices[:,i] == 0) | (all_indices[:,i] == self.arr.shape[i]-1)

        good_indices = all_indices[is_good]
        return np.any(self.arr[good_indices])

    # This uses the centers as measurement
    def indices_within_x_of(self, _x, pt):
        low = pt - _x
        high = pt + _x

        # If you hit these, you are about to make a mistake
        assert( not np.any( low <= self.lb + self.cs))
        assert( not np.any( high >= self.ub - self.cs ) )

        bounds = self.floats_to_indices( np.array( [low, high] ) )

        ranges = []
        size = 1
        for i in range(self.dim):
            ranges.append(np.arange(bounds[0, i], bounds[1, i] + 1) )
            size *= (len(ranges[-1]))
        ranges = np.array(ranges)

        #in numba version, this whole bottom part is tested for loops

        # indices = np.array(itertools.product(*ranges))
        indices = np.array(np.meshgrid(*ranges)).T.reshape(-1, len(ranges))

        centers = self.indices_to_centers(indices)

        return indices[ np.sum(np.square(centers - pt), axis=-1) < _x*_x ]

    def dump_mask_true(self, fname, mask, resname="VOX", atname="VOXL", z=None, fraction=1 ):

        indices = np.array(list(np.where(mask))).T
        centers = self.indices_to_centers(indices)

        if ( self.dim == 2 ):
            centers_ = np.zeros((len(centers), 3), np.float)
            centers_[:,:2] = centers
            centers_[:,2] = z
            centers = centers_

        if ( fraction < 1 ):
            mask = np.random.random(len(indices)) < fraction
            # indices = indices[mask]
            centers = centers[mask]

        f = open(fname, "w")

        anum = 1
        rnum = 1

        for ind, xyz in enumerate(centers):

            f.write("%s%5i %4s %3s %s%4i    %8.3f%8.3f%8.3f%6.2f%6.2f %11s\n"%(
                "HETATM",
                anum,
                atname,
                resname,
                "A",
                rnum,
                xyz[0],xyz[1],xyz[2],
                1.0,
                1.0,
                "HB"
                ))

            anum += 1
            rnum += 1
            anum %= 100000
            rnum %= 10000

        f.close()

    def dump_grids_true(self, fname, func, resname="VOX", atname="VOXL", jitter=False, z=None):
        centers = self.all_centers()
        vals = self.arr[tuple(self.floats_to_indices(centers).T)]

        if ( self.dim == 2 ):
            centers_ = np.zeros((len(centers), 3), np.float)
            centers_[:,:2] = centers
            centers_[:,2] = z
            centers = centers_


        f = open(fname, "w")

        anum = 1
        rnum = 1

        for ind, xyz in enumerate(centers):
            if ( jitter ):
                xyz[0] += 0.01*2*(1 - 0.5*random.random())
                xyz[1] += 0.01*2*(1 - 0.5*random.random())
                xyz[2] += 0.01*2*(1 - 0.5*random.random())
            val = vals[ind]
            if (not func(val)):
                continue

            f.write("%s%5i %4s %3s %s%4i    %8.3f%8.3f%8.3f%6.2f%6.2f %11s\n"%(
                "HETATM",
                anum,
                atname,
                resname,
                "A",
                rnum,
                xyz[0],xyz[1],xyz[2],
                1.0,
                1.0,
                "HB"
                ))

            anum += 1
            rnum += 1
            anum %= 100000
            rnum %= 10000

        f.close()

    def clash_check(self, pts, max_clashes):
        assert(self.dim == 3)

        return numba_clash_check(pts, max_clashes, self.arr, self.lb, self.cs)

    def ray_trace(self, start, end, max_clashes, debug=False):
        assert(self.dim == 3)

        return numba_ray_trace(start, end, max_clashes, self.arr, self.lb, self.cs, debug)

    def ray_trace_many(self, starts, ends, max_clashes, debug=False):
        assert(self.dim == 3)

        return numba_ray_trace_many(starts, ends, max_clashes, self.arr, self.lb, self.cs, debug)

    def add_to_clashgrid(self, pts, atom_radius, store_val=True ):
        if ( isinstance( atom_radius, list ) ):
            assert(len(pts) == len(atom_radius))
            numba_make_clashgrid_var_atom_radius(pts, atom_radius, self.arr, self.lb, self.ub, self.cs, self.arr.shape, store_val)
        else:
            numba_make_clashgrid(pts, atom_radius, self.arr, self.lb, self.ub, self.cs, self.arr.shape, store_val)


    def add_to_sum_grid(self, pts, atom_radius, store_val=1 ):
        numba_make_sum_grid(pts, atom_radius, self.arr, self.lb, self.ub, self.cs, self.arr.shape, store_val)


    # fill the voxel array with ipt for all voxels closest to ipt.
    # initialize self to -1 and dist_grid to +100000
    def add_to_near_grid(self, pts, atom_radius, dist_grid, store_vals = None):
        assert((self.lb == dist_grid.lb).all())
        assert((self.ub == dist_grid.ub).all())
        assert((self.cs == dist_grid.cs).all())
        assert(self.arr.shape == dist_grid.arr.shape)
        if ( store_vals is None ):
            store_vals = np.arange(len(pts))
        numba_add_to_near_grid(pts, store_vals, atom_radius, self.arr, dist_grid.arr, self.lb, self.ub, self.cs, self.arr.shape)


    # fill voxels with -1 if below surface, 1 if above
    def do_surface_crawl(self, start, normal, direction, distance):
        return numba_do_surface_crawl(start, normal, direction, distance, self.arr, self.lb, self.ub, self.cs, self.arr.shape)

    def flood_fill(self, fill_val, overwrite_val):
        if ( self.dim == 2 ):
            return numba_flood_fill_2d(fill_val, overwrite_val, self.arr, self.lb, self.ub, self.cs, self.arr.shape )
        if ( self.dim == 3 ):
            return numba_flood_fill_3d(fill_val, overwrite_val, self.arr, self.lb, self.ub, self.cs, self.arr.shape )
        assert(False)

    def flood_fill_from_here(self, fill_val, overwrite_val, start_idx):
        return numba_flood_fill_3d_from_here(fill_val, overwrite_val, start_idx, self.arr, self.lb, self.ub, self.cs, self.arr.shape)

def from_vector(vec):
    xyz = np.array([0, 0, 0]).astype(float)
    xyz[0] = vec.x
    xyz[1] = vec.y
    xyz[2] = vec.z
    return xyz
# Developability index: a rapid in silico tool for the screening of antibody aggregation propensity.
def sap_score(pose, radius, name_no_suffix, out_score_map, out_string_map,
        suffix, zero_adjust):
    # R from the paper is 5
    R = radius
    # Development of hydrophobicity parameters to analyze proteins which bear post- or cotranslational modifications
    # then you subtract 0.5 from scaled
    hydrophobicity = {
        'A': 0.116,
        'C': 0.18,
        'D': -0.472,
        'E': -0.457,
        'F': 0.5,
        'G': 0.001,
        'H': -0.335,
        'I': 0.443,
        'K': -0.217,
        'L': 0.443,
        'M': 0.238,
        'N': -0.264,
        'P': 0.211,
        'Q': -0.249,
        'R': -0.5,
        'S': -0.141,
        'T': -0.05,
        'V': 0.325,
        'W': 0.378,
        'Y': 0.38,
    }
    max_sasa = {
            'A': 60.92838710394679, 
            'C': 87.69781472008721, 
            'D': 92.90391166066215, 
            'E': 122.88603427090675, 
            'F': 169.3483818866212, 
            'G': 0, 
            'H': 145.4356924808598, 
            'I': 138.7893503649479, 
            'K': 166.59514526133574, 
            'L': 139.23818059219622, 
            'M': 144.91845035026915, 
            'N': 102.23351599614529, 
            'P': 97.53505848923146, 
            'Q': 125.3684583287609, 
            'R': 186.1814228248932, 
            'S': 69.12103617846579, 
            'T': 96.34149409391819, 
            'V': 112.61829513370847, 
            'W': 193.23465173310578, 
            'Y': 176.41705079710476
            }
    surf_vol = get_per_atom_sasa(pose)
    # get the per res base stats
    res_max_sasa = [None]
    res_hydrophobicity = [None]

    for resnum in range(1, pose.size()+1):
        letter = pose.residue(resnum).name1()
        res_max_sasa.append(max_sasa[letter])
        res_hydrophobicity.append(hydrophobicity[letter] + zero_adjust)
    # make the things required to find 5A neighbors 
    idx_to_atom = []
    xyzs = []
    atom_sasa = []
    for resnum in range(1, pose.size()+1):
        res = pose.residue(resnum)
        for at in range(1, res.natoms()+1):
            if ( res.atom_is_backbone(at) ):
                continue
            xyzs.append(from_vector(res.xyz(at)))
            idx_to_atom.append([resnum, at])
            atom_sasa.append(surf_vol.surf(resnum, at))
    
    atom_sasa = np.array(atom_sasa)
    idx_to_atom = np.array(idx_to_atom)
    xyzs = np.array(xyzs)

    resl = 1

    low = np.min(xyzs, axis=0) - R*2 - resl*2
    high = np.max(xyzs, axis=0) + R*2 + resl*2

    print("Making neighbor grid")
    clashgrid = VoxelArray(low, high, np.array([resl]*3), object)
    for idx, _ in enumerate(clashgrid.arr.flat):
        clashgrid.arr.flat[idx] = []

    for ixyz, xyz in enumerate(xyzs):
        indices = clashgrid.indices_within_x_of(R+resl, xyz)
        for index in indices:
            clashgrid.arr[tuple(index)].append(ixyz)

    atom_grid_indices = clashgrid.floats_to_indices(xyzs)

    sap_scores = []

    pdb_info = pose.pdb_info()

    for iatom in range(len(xyzs)):
        xyz = xyzs[iatom]
        resnum, at = idx_to_atom[iatom]
        grid_index = atom_grid_indices[iatom]

        grid_list = np.array(list(clashgrid.arr[tuple(grid_index)]))

        distances = np.linalg.norm( xyzs[grid_list] - xyz, axis=-1)

        idx_within_R = grid_list[distances <= R]

        atoms_within_R = idx_to_atom[idx_within_R]
        resnums = np.unique(atoms_within_R[:,0])

        atom_score = 0
        for ot_resnum in resnums:
            ats_idx = idx_within_R[atoms_within_R[:,0] == ot_resnum]
            res_sasa = np.sum(atom_sasa[ats_idx])

            res_score = res_sasa / res_max_sasa[ot_resnum] * res_hydrophobicity[ot_resnum]
            if ( res_score > 1000 ):
                print(ot_resnum, pose.residue(ot_resnum).name1(),
                        res_sasa, res_max_sasa[ot_resnum], 
                        res_hydrophobicity[ot_resnum])

            atom_score += res_score

        pdb_info.bfactor(resnum, at, atom_score)
        sap_scores.append(atom_score)

    sap_scores = np.array(sap_scores)

    sap_score = np.sum( sap_scores[sap_scores > 0])
    print("sap score: %.1f"%sap_score)
    out_score_map['sap_score'] = sap_score
    return pose

def sfxn_hard_maker(const_bb=True, up_ele=False) -> ScoreFunction:
    """Sets up Bcov's reweighted score function that penalizes buried
    unsatisfied polars more highly so that Rosetta doesn't make as many
    mistakes. Also unsets lk_ball because lk_ball is slow. 
    
    Args:
        const_bb (bool): Set this to False if you don't know where to expect
        PRO. Sets approximate_buried_unsat_penalty_assume_const_backbone.
        up_ele (bool): Increase the bonus for electrostatics, good for making
        Rosetta design salt bridges and hydrogen bonds.

    Returns:
        sfxn_hard (ScoreFunction): The modified score function.
    
    TODO: 
        as of 08172020 there is not much evidence that this reweighting is 
        necessary, but it doesn't seem to harm anything. 
        approximate_buried_unsat_penalty_hbond_energy_threshold may depend, 
        -0.5 is default but -0.2/-0.25 might work too. Might be good to check
        if it is really a good idea to unset lk_ball. Current best practices 
        are to set beta16_nostab.wts as the weights. Hopefully beta_nov20 
        will be much better. For sequence conservation, currently constraints
        (res_type_constraint) is set externally and this seems to work fine.
        as of 08262020 I am not sure if up_ele is considered best practices, 
        but since this implementation is primarily intended for resurfacing I
        think it should be okay to leave in for now.
    """
    sfxn_hard = pyrosetta.create_score_function("beta_nov16.wts")
    sfxn_hard.set_weight(ScoreType.aa_composition, 1.0)
    sfxn_hard.set_weight(ScoreType.approximate_buried_unsat_penalty, 5.0)
    emo = sfxn_hard.energy_method_options()
    # shallower cutoff of 3 as opposed to 4, which is usual. 
    emo.approximate_buried_unsat_penalty_burial_atomic_depth(3.0)
    emo.approximate_buried_unsat_penalty_hbond_energy_threshold(-0.2)	
    if const_bb:
        emo.approximate_buried_unsat_penalty_assume_const_backbone(1)
    else:
        emo.approximate_buried_unsat_penalty_assume_const_backbone(0)
    if up_ele:
        sfxn_hard.set_weight(ScoreType.fa_elec, 1.4)
        sfxn_hard.set_weight(ScoreType.hbond_sc, 2.0)
    else:
        pass
    sfxn_hard.set_energy_method_options(emo)
    sfxn_hard.set_weight(ScoreType.lk_ball, 0)
    sfxn_hard.set_weight(ScoreType.lk_ball_iso, 0)
    sfxn_hard.set_weight(ScoreType.lk_ball_bridge, 0)
    sfxn_hard.set_weight(ScoreType.lk_ball_bridge_uncpl, 0)
    return sfxn_hard

def generic_layer_dict_maker() -> dict:
    """Just a function that puts all of the standard layer definitions and 
    their corresponding allowed amino acids into a convenient dictionary.
    As of versions > 0.7.0, made layers more restrictive. Old definitions 
    for helices were: 
    "core AND helix": 'AFILVWYNQHM',
    "boundary AND helix_start": 'ADEHIKLNPQRSTVY',
    "boundary AND helix": 'ADEHIKLNQRSTVYM'
    As of versions > 0.8.0, made layers slightly less restrictive. Old 
    definitions for helices were: 
    "core AND helix": 'AILVYNQHM',
    "boundary AND helix_start": 'ADEHKNPQRST',
    "boundary AND helix": 'ADEHKNQRST'
    Args:
        None

    Returns:
        layer_dict (dict): The dict mapping standard layer definitions to 
        their allowed amino acids.
    
    """
    layer_dict = {"core AND helix_start": 'AFILVWYNQSTHP',
                  "core AND helix": 'AFILVYNQHM',
                  "core AND loop": 'AFGILPVWYDENQSTHM',
                  "core AND sheet": 'FILVWYDENQSTH',
                  "boundary AND helix_start": 'ADEHIKLNPQRSTV',
                  "boundary AND helix": 'ADEHIKLNQRSTVM',
                  "boundary AND loop": 'ADEFGHIKLNPQRSTVY',
                  "boundary AND sheet": 'DEFHIKLNQRSTVY',
                  "surface AND helix_start": 'DEHKPQR',
                  "surface AND helix": 'EHKQR',
                  "surface AND loop": 'DEGHKNPQRST',
                  "surface AND sheet": 'EHKNQRST',
                  "helix_cap": 'DNSTP'}
    return layer_dict

def layer_design_maker(cutoffs:tuple, use_dssp:bool, use_sc_neighbors:bool
                      ) -> operation.DesignRestrictions:
    """Given options, returns a layer design task operation.

    Args:
        cutoffs (tuple): The tuple to set the number of sidechain neighbors or
        SASA accessibility. cutoffs[0] is for core, cuttoffs[1] is for surface
        and everything in between is considered boundary. 
        use_dssp (bool): Whether to use DSSP to determine secondary structure. 
        This is probably a good idea since sometimes poses don't have info on 
        secondary structure.
        use_sc_neighbors: Whether to use the number of sidechain neighbors to 
        determine burial. If false, will use SASA to determine burial. 
        Sidechain neighbors seems a little less noisy.
        
    Returns:
        layer_design (operation.DesignRestrictions): The task operation set by
        the options, ready to be passed to a task factory. 
    """
    # setup residue selectors: find surfaces, boundaries, cores, ss elements 
    surface = residue_selector.LayerSelector()
    surface.set_layers(0, 0, 1), surface.set_cutoffs(*cutoffs)
    surface.set_use_sc_neighbors(int(use_sc_neighbors))
    boundary = residue_selector.LayerSelector()
    boundary.set_layers(0, 1, 0), boundary.set_cutoffs(*cutoffs)
    boundary.set_use_sc_neighbors(int(use_sc_neighbors))
    core = residue_selector.LayerSelector()
    core.set_layers(1, 0, 0), core.set_cutoffs(*cutoffs)
    core.set_use_sc_neighbors(int(use_sc_neighbors))
    sheet = residue_selector.SecondaryStructureSelector("E")
    sheet.set_overlap(0)
    sheet.set_minH(3), sheet.set_minE(3)
    sheet.set_include_terminal_loops(0)
    sheet.set_use_dssp(int(use_dssp))
    entire_loop = residue_selector.SecondaryStructureSelector("L")
    entire_loop.set_overlap(0)
    entire_loop.set_minH(3), entire_loop.set_minE(3)
    entire_loop.set_include_terminal_loops(1)
    entire_loop.set_use_dssp(int(use_dssp))
    entire_helix = residue_selector.SecondaryStructureSelector("H")
    entire_helix.set_overlap(0)
    entire_helix.set_minH(3), entire_helix.set_minE(3)
    entire_helix.set_include_terminal_loops(0)
    entire_helix.set_use_dssp(int(use_dssp))
    lower_helix = PrimarySequenceNeighborhoodSelector(1, 0, entire_helix)
    helix_cap = AndResidueSelector(entire_loop, lower_helix)
    upper_helix_cap = PrimarySequenceNeighborhoodSelector(0, 1, helix_cap)
    helix_start = AndResidueSelector(entire_helix, upper_helix_cap)
    not_helix_start = NotResidueSelector(helix_start)
    helix = AndResidueSelector(entire_helix, not_helix_start)
    not_helix_cap = NotResidueSelector(helix_cap)
    loop = AndResidueSelector(entire_loop, not_helix_cap)
    # setup layer design
    layer_dict = generic_layer_dict_maker()
    layer_design = operation.DesignRestrictions()
    for selector_logic, aas in layer_dict.items():
        rlto = operation.RestrictAbsentCanonicalAASRLT()
        rlto.aas_to_keep(aas)
        selector_objs = []
        if 'AND' in selector_logic:
            selectors = selector_logic.split(" AND ")
            for selector_str in selectors:
                selector_objs.append(eval(selector_str))
            selector = AndResidueSelector(*tuple(selector_objs))
        elif 'OR' in selector_logic:
            selectors = selector_logic.split(" OR ")
            for selector_str in selectors:
                selector_objs.append(eval(selector_str))
            selector = OrResidueSelector(*tuple(selector_objs))
        else:
            selector = eval(selector_logic)
        layer_design.add_selector_rlto_pair(selector, rlto)
    return layer_design

def design_pack_lock_maker(design_resis:list) -> tuple:
    """Given options, returns a design, pack, and lock task operations.

    Args:
        design_resis (list): A list of residues to design. Neighbors will be 
        allowed to repack, everything else will be locked. Cysteine is not 
        allowed as a residue for design, everything else is, so should be used
        in combination with other task operations. 
        
    Returns:
        design, pack, lock (tuple): The task operations set by the options,
        ready to be passed to a task factory. 
    """
    designable = ResidueIndexSelector()
    designable.set_index(','.join([str(x) for x in design_resis]))
    not_designable = NotResidueSelector()
    not_designable.set_residue_selector(designable)
    packable = NeighborhoodResidueSelector()
    packable.set_focus_selector(designable)
    not_packable = NotResidueSelector()
    not_packable.set_residue_selector(packable)
    no_cys = operation.RestrictAbsentCanonicalAASRLT()
    no_cys.aas_to_keep('ADEFGHIKLMNPQRSTVWY')
    no_design = operation.RestrictToRepackingRLT()
    no_repack = operation.PreventRepackingRLT()
    design = operation.OperateOnResidueSubset(no_cys, designable)
    pack = operation.OperateOnResidueSubset(no_design, not_designable)
    lock = operation.OperateOnResidueSubset(no_repack, not_packable)
    return design, pack, lock

def disfavor_native_residue_maker(sfxn: ScoreFunction, restraint: float
                              ) -> FavorNativeResidue:
    """Given options, makes rosetta penalize the native residues. It can also
    incentivize using native residues if it is passed a positive restraint.

    Args:
        sfxn (ScoreFunction): A Rosetta ScoreFunction. It will have the weight
        for the res_type_constraint set to the restraint value.
        restraint (float): What weight set res_type_constraint to. A positive 
        value will reward native sequence conservation, a negative value will
        penalize it.
        
    Returns:
        disfavor_native_residue (FavorNativeResidue): The the 
        FavorNativeResidue mover set by the options, ready to be applied to a 
        pose. 
    """
    sfxn.set_weight(ScoreType.res_type_constraint, restraint)
    xml_string = """
    <MOVERS>
        <FavorSequenceProfile name="disfavor" weight="1" 
        use_current="true" matrix="IDENTITY"/>
    </MOVERS>
    """
    xml_obj = XmlObjects.create_from_string(xml_string)
    disfavor_native_residue = xml_obj.get_mover('disfavor')
    return disfavor_native_residue

def relax_script_maker(relax_script:str
                      ) -> pyrosetta.rosetta.std.vector_std_string():
    """Given an absolute or local path or a database relax script name, sets
    up a relax script for rosetta to read in after reading it in line by line.

    Args:
        relax_script (str): Somewhat flexibly implemented and can be an 
        absolute or local path or a database relax script
        
    Returns:
        script (pyrosetta.rosetta.std.vector_std_string): The relax script,
        ready to read into a mover. 
    """
    script = pyrosetta.rosetta.std.vector_std_string()
    path = "/software/rosetta/latest/database/sampling/relax_scripts/"
    # assumes database script if only the base name of a script is given
    if ('/' not in relax_script and ".txt" not in relax_script):
        absolute_path = path + relax_script + ".txt"
    # assumes database script if the name of a script is given without a path
    elif ('/' not in relax_script):
        absolute_path = path + relax_script
    # if there is a full name and path assumes a custom script
    else: 
        absolute_path = relax_script
    with open(absolute_path) as f:
        lines = f.readlines()
        for line in lines:
            script.append(' '.join(line.split()))
    return script

# TODO documentation
def fast_design_with_options(pose:Pose, to_design=[], cutoffs=(20,40), 
        flexbb=True, relax_script="MonomerDesign2019", restraint=0,
        up_ele=False, use_dssp=True, use_sc_neighbors=True) -> Pose:
    """
    """
    sfxn_hard = sfxn_hard_maker(up_ele=up_ele)
    # determine which residues are designable
    true_sel = residue_selector.TrueResidueSelector()
    if len(to_design) == 0:
        design_resis = list(get_residues_from_subset(true_sel.apply(pose)))
    else:
        design_resis = to_design.copy()
    # setup task operations
    task_factory = pyrosetta.rosetta.core.pack.task.TaskFactory()
    design, pack, lock = design_pack_lock_maker(design_resis)
    layer_design = layer_design_maker(cutoffs, use_dssp, use_sc_neighbors)
    ic = operation.IncludeCurrent()
    arochi = LimitAromaChi2Operation()
    arochi.include_trp(True)
    ex1_ex2 = operation.ExtraRotamersGeneric()
    ex1_ex2.ex1(True), ex1_ex2.ex2(True)
    prune = PruneBuriedUnsatsOperation()
    for op in [design, pack, lock, layer_design, ic, arochi, ex1_ex2, prune]:
        task_factory.push_back(op)
    # setup movemap
    mm = pyrosetta.rosetta.core.kinematics.MoveMap()
    mm.set_bb(flexbb), mm.set_chi(True), mm.set_jump(False)
    # optionally enable FNR
    if restraint != 0:
        disfavor_native_residue = disfavor_native_residue_maker(
                sfxn=sfxn_hard, restraint=restraint)
        disfavor_native_residue.apply(pose)
    else:
        pass
    # setup fast design
    fast_design = FastDesign(scorefxn_in=sfxn_hard, standard_repeats=1)
    script = relax_script_maker(relax_script)
    fast_design.set_script_from_lines(script)
    fast_design.cartesian(False)
    fast_design.set_task_factory(task_factory)
    fast_design.set_movemap(mm)
    fast_design.constrain_relax_to_start_coords(True)
    fast_design.minimize_bond_angles(False)
    fast_design.minimize_bond_lengths(False)
    fast_design.min_type("lbfgs_armijo_nonmonotone")
    fast_design.ramp_down_constraints(False)
    fast_design.apply(pose)
    return pose

def less_ARG_maker():# -> AddCompositionConstraintMover:
    """Makes a composition constraint that adds a 1 REU penalty for each ARG
    that is introduced.

    Args:
    None        

    Returns:
    less_ARG (AddCompositionConstraintMover): The compositions constraint
    mover, ready to be applied to a pose.

    """
    xml_string = """
    <MOVERS>
        <AddCompositionConstraintMover name="penalty">
            <Comp entry="
                PENALTY_DEFINITION;
                TYPE ARG; 
                ABSOLUTE 1; 
                DELTA_START -1; 
                DELTA_END 1;
                PENALTIES 0 1 1.5;
                BEFORE_FUNCTION LINEAR; 
                AFTER_FUNCTION LINEAR;
                END_PENALTY_DEFINITION;" />
        </AddCompositionConstraintMover>
    </MOVERS>
    """
    xml_obj = XmlObjects.create_from_string(xml_string)
    less_ARG = xml_obj.get_mover('penalty')
    return less_ARG
    
def residue_sap_list_maker(pose:Pose) -> list:
    residue_sap_list = []
    for resi in range(1, pose.size()+1):
        residue = pose.residue(resi)
        residue_sap = 0
        for atom in range(1, residue.natoms()+1):
            if residue.atom_is_backbone(atom):
                continue
            else:
                residue_sap += pose.pdb_info().bfactor(resi, atom)
        residue_sap_list.append((resi, residue_sap))
        
    return residue_sap_list

def get_per_atom_sasa(pose, probe_size=1.1):
    atoms = core.id.AtomID_Map_bool_t()
    atoms.resize(pose.size())
    for i in range(1, pose.size()+1):
        atoms.resize( i, pose.residue(i).natoms(), True)
    surf_vol = core.scoring.packing.get_surf_vol( pose, atoms, probe_size)
    return surf_vol

def fix_scorefxn(sfxn, allow_double_bb=False):
    opts = sfxn.energy_method_options()
    opts.hbond_options().decompose_bb_hb_into_pair_energies(True)
    opts.hbond_options().bb_donor_acceptor_check(not allow_double_bb)
    opts.hbond_options().use_hb_env_dep(True)
    #opts.elec_context_dependent(True)

    sfxn.set_energy_method_options(opts)


def my_rstrip(string, strip):
    if (string.endswith(strip)):
        return string[:-len(strip)]
    return string
# TODO implement actual main? Begin Main:
def main():
    if len(sys.argv) == 1:
        parser.print_help()
    args = parser.parse_args(sys.argv[1:])
    print("Redesign will proceed with the following options:")
    print(args)
    pdbs = args.pdbs
    silent = args.__getattribute__("in:file:silent")
    worst_n = args.worst_n
    zero_adjust = args.zero_adjust
    radius = args.radius
    flexbb = args.flexbb
    use_sasa = args.use_sasa
    lock_resis = args.lock_resis
    cutoffs = tuple(args.cutoffs)
    relax_script = args.relax_script
    up_ele = args.up_ele
    prescore = args.prescore
    rescore = args.rescore
    chunk = args.chunk
    lock_HNQST = args.lock_HNQST
    lock_PG = args.lock_PG
    lock_YW = args.lock_YW
    penalize_ARG = args.penalize_ARG
    encourage_mutation = args.encourage_mutation
    restraint_weight = args.restraint_weight
    redesign_above = args.redesign_above
    redesign_below = args.redesign_below
    # a hack to avoid changing a lot of code don't judge
    use_sc_neighbors = not use_sasa
    if silent is None:
        silent = ''
    # TODO add more defense here

    if (silent != ''):
        sfd_in = SilentFileData(SilentFileOptions())
        sfd_in.read_file(silent)
        pdbs = list(sfd_in.tags())
        sfd_out = SilentFileData("out.silent", False, False, 'binary',
                SilentFileOptions())
    for pdb in pdbs:
        t0 = time.time()
        print("Attempting pose: " + pdb)
        for k in [1]:
            if (silent == ''):
                pose = pose_from_file(pdb)
            else:
                pose = Pose()
                sfd_in.get_structure(pdb).fill_pose(pose)

            name_no_suffix = my_rstrip(my_rstrip(os.path.basename(pdb),
                ".gz"), ".pdb")
            score_map = std.map_std_string_double()
            string_map = std.map_std_string_std_string()
            if prescore:
                # get SAP score for the pose
                print("Prescoring SAP:")
                pre_pose = sap_score(pose, radius, name_no_suffix, score_map,
                        string_map, '', zero_adjust)
            else:
                # if prescore is set to false, assumes pose b-factors have score
                pre_pose = pose.clone()
            # use per residue SAP to make a list of the worst offenders
            residue_sap_list = residue_sap_list_maker(pre_pose)
            sorted_residue_sap_list = sorted(residue_sap_list, 
                    key=lambda x: x[1], reverse=True)
            if lock_HNQST:
                # get list of HIS, ASN, GLN, SER, THR positions
                H_sel = ResidueNameSelector()
                H_sel.set_residue_name3('HIS')
                N_sel = ResidueNameSelector()
                N_sel.set_residue_name3('ASN')
                Q_sel = ResidueNameSelector()
                Q_sel.set_residue_name3('GLN')
                S_sel = ResidueNameSelector()
                S_sel.set_residue_name3('SER')
                T_sel = ResidueNameSelector()
                T_sel.set_residue_name3('THR')
                HN_sel = OrResidueSelector(H_sel, N_sel)
                QS_sel = OrResidueSelector(Q_sel, S_sel)
                HNQS_sel = OrResidueSelector(HN_sel, QS_sel)
                the_sel = OrResidueSelector(HNQS_sel, T_sel)
                the_list = list(get_residues_from_subset(
                    the_sel.apply(pre_pose)))
                # combine into locked resis
                lock_resis.extend(the_list)
                lock_resis = list(set(lock_resis))
            else:
                pass
            if lock_PG:
                # get list of PRO and GLY positions
                P_sel = ResidueNameSelector()
                P_sel.set_residue_name3('PRO')
                G_sel = ResidueNameSelector()
                G_sel.set_residue_name3('GLY')
                PG_sel = OrResidueSelector(P_sel, G_sel)
                PG_list = list(get_residues_from_subset(
                    PG_sel.apply(pre_pose)))
                # combine into locked resis
                lock_resis.extend(PG_list)
                lock_resis = list(set(lock_resis))
            else:
                pass
            if lock_YW:
                # get list of TYR and TRP positions
                Y_sel = ResidueNameSelector()
                Y_sel.set_residue_name3('TYR')
                W_sel = ResidueNameSelector()
                W_sel.set_residue_name3('TRP')
                YW_sel = OrResidueSelector(Y_sel, W_sel)
                YW_list = list(get_residues_from_subset(
                    YW_sel.apply(pre_pose)))
                # combine into locked resis
                lock_resis.extend(YW_list)
                lock_resis = list(set(lock_resis))
            else:
                pass
            # check to see if each worst resi is allowed to be designed
            print("Residues that will NOT be designed:",
                        ' '.join(str(x) for x in lock_resis))
            worst_resis = []
            for residue, residue_score in sorted_residue_sap_list:
                if len(worst_resis) >= worst_n:
                    break
                else:
                    pass
                print("Residue:", residue, "Score:", residue_score)
                if residue in lock_resis:
                    continue
                elif (
                        redesign_above is not None
                        and residue_score < redesign_above
                        ):
                    continue
                elif (
                        redesign_below is not None
                        and residue_score > redesign_below
                        ):
                    continue
                else:
                    worst_resis.append(residue)
                    print("Residue {0} added for redesign".format(residue))
            print("Residues that will be designed:",
                    ' '.join(str(x) for x in worst_resis))
            if encourage_mutation:
                restraint = restraint_weight
            else:
                restraint = 0
            # redesign a new pose targeting the worst residues
            new_pose = pre_pose.clone()
            if penalize_ARG:
                less_ARG = less_ARG_maker()
                less_ARG.apply(new_pose)
            else:
                pass
            if chunk:
                chunk_resis_list = [worst_resis[x:x+10] for x in range(0, len(
                    worst_resis), 10)]
                for chunk_resis in chunk_resis_list:
                    new_pose = fast_design_with_options(new_pose,
                            to_design=chunk_resis, cutoffs=cutoffs, 
                            flexbb=flexbb, relax_script=relax_script,
                            restraint=restraint, up_ele=up_ele, use_dssp=True,
                            use_sc_neighbors=use_sc_neighbors)
                    if penalize_ARG:
                        less_ARG.apply(new_pose)
                    else:
                        pass
            else:
                new_pose = fast_design_with_options(new_pose,
                        to_design=worst_resis, cutoffs=cutoffs, flexbb=flexbb,
                        relax_script=relax_script, restraint=restraint, 
                        up_ele=up_ele, use_dssp=True,
                        use_sc_neighbors=use_sc_neighbors)
            name_no_suffix += '_resurf'
            if rescore:
                # rescore the designed pose
                print("Rescoring SAP:")
                post_pose = sap_score(new_pose, radius, name_no_suffix, 
                        score_map, string_map, '', zero_adjust)
            else:
                post_pose = new_pose.clone()
            if (pre_pose != None):
                if (silent == ''):
                    post_pose.dump_pdb(name_no_suffix + ".pdb")
                else:
                    struct = sfd_out.create_SilentStructOP()
                    struct.fill_struct(post_pose, name_no_suffix)
                    sfd_out.add_structure(struct)

            seconds = int(time.time() - t0)
            print("protocols.jd2.JobDistributor: {0} reported success in {1} \
                    seconds".format(name_no_suffix, seconds))

    if (silent != ''):
        sfd_out.write_all("out.silent", False)
if __name__ == "__main__":
    main()
