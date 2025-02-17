"""Contains common utility functions."""
#  Copyright (c) 2018 PaddlePaddle Authors. All Rights Reserve.
#
#Licensed under the Apache License, Version 2.0 (the "License");
#you may not use this file except in compliance with the License.
#You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
#Unless required by applicable law or agreed to in writing, software
#distributed under the License is distributed on an "AS IS" BASIS,
#WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#See the License for the specific language governing permissions and
#limitations under the License.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import distutils.util
import os
import numpy as np
import six
import requests
import shutil
import tqdm
import hashlib
import tarfile
import zipfile
import logging
import paddle.fluid as fluid
import paddle.compat as cpt
from paddle.fluid import core
from paddle.fluid.framework import Program

logging.basicConfig(format='%(asctime)s-%(levelname)s: %(message)s')
_logger = logging.getLogger(__name__)
_logger.setLevel(logging.INFO)

DOWNLOAD_RETRY_LIMIT=3

def print_arguments(args):
    """Print argparse's arguments.
    Usage:
    .. code-block:: python
        parser = argparse.ArgumentParser()
        parser.add_argument("name", default="Jonh", type=str, help="User name.")
        args = parser.parse_args()
        print_arguments(args)
    :param args: Input argparse.Namespace for printing.
    :type args: argparse.Namespace
    """
    print("-----------  Configuration Arguments -----------")
    for arg, value in sorted(six.iteritems(vars(args))):
        print("%s: %s" % (arg, value))
    print("------------------------------------------------")


def add_arguments(argname, type, default, help, argparser, **kwargs):
    """Add argparse's argument.
    Usage:
    .. code-block:: python
        parser = argparse.ArgumentParser()
        add_argument("name", str, "Jonh", "User name.", parser)
        args = parser.parse_args()
    """
    type = distutils.util.strtobool if type == bool else type
    argparser.add_argument(
        "--" + argname,
        default=default,
        type=type,
        help=help + ' Default: %(default)s.',
        **kwargs)


def save_persistable_nodes(executor, dirname, graph):
    """
    Save persistable nodes to the given directory by the executor.
    Args:
        executor(Executor): The executor to run for saving node values.
        dirname(str): The directory path.
        graph(IrGraph): All the required persistable nodes in the graph will be saved.
    """
    persistable_node_names = set()
    persistable_nodes = []
    all_persistable_nodes = graph.all_persistable_nodes()
    for node in all_persistable_nodes:
        name = cpt.to_text(node.name())
        if name not in persistable_node_names:
            persistable_node_names.add(name)
            persistable_nodes.append(node)
    program = Program()
    var_list = []
    for node in persistable_nodes:
        var_desc = node.var()
        if var_desc.type() == core.VarDesc.VarType.RAW or \
                var_desc.type() == core.VarDesc.VarType.READER:
            continue
        var = program.global_block().create_var(
            name=var_desc.name(),
            shape=var_desc.shape(),
            dtype=var_desc.dtype(),
            type=var_desc.type(),
            lod_level=var_desc.lod_level(),
            persistable=var_desc.persistable())
        var_list.append(var)
    fluid.io.save_vars(executor=executor, dirname=dirname, vars=var_list)


def load_persistable_nodes(executor, dirname, graph):
    """
    Load persistable node values from the given directory by the executor.
    Args:
        executor(Executor): The executor to run for loading node values.
        dirname(str): The directory path.
        graph(IrGraph): All the required persistable nodes in the graph will be loaded.
    """
    persistable_node_names = set()
    persistable_nodes = []
    all_persistable_nodes = graph.all_persistable_nodes()
    for node in all_persistable_nodes:
        name = cpt.to_text(node.name())
        if name not in persistable_node_names:
            persistable_node_names.add(name)
            persistable_nodes.append(node)
    program = Program()
    var_list = []

    def _exist(var):
        return os.path.exists(os.path.join(dirname, var.name))

    def _load_var(name, scope):
        return np.array(scope.find_var(name).get_tensor())

    def _store_var(name, array, scope, place):
        tensor = scope.find_var(name).get_tensor()
        tensor.set(array, place)

    for node in persistable_nodes:
        var_desc = node.var()
        if var_desc.type() == core.VarDesc.VarType.RAW or \
                var_desc.type() == core.VarDesc.VarType.READER:
            continue
        var = program.global_block().create_var(
            name=var_desc.name(),
            shape=var_desc.shape(),
            dtype=var_desc.dtype(),
            type=var_desc.type(),
            lod_level=var_desc.lod_level(),
            persistable=var_desc.persistable())
        if _exist(var):
            var_list.append(var)
        else:
            _logger.info("Cannot find the var %s!!!" % (node.name()))
    fluid.io.load_vars(executor=executor, dirname=dirname, vars=var_list)


def _download(url, path, md5sum=None):
    """
    Download from url, save to path.
    url (str): download url
    path (str): download to given path
    """
    if not os.path.exists(path):
        os.makedirs(path)

    fname = os.path.split(url)[-1]
    fullname = os.path.join(path, fname)
    retry_cnt = 0

    while not (os.path.exists(fullname) and _md5check(fullname, md5sum)):
        if retry_cnt < DOWNLOAD_RETRY_LIMIT:
            retry_cnt += 1
        else:
            raise RuntimeError("Download from {} failed. "
                               "Retry limit reached".format(url))

        _logger.info("Downloading {} from {}".format(fname, url))

        req = requests.get(url, stream=True)
        if req.status_code != 200:
            raise RuntimeError("Downloading from {} failed with code "
                               "{}!".format(url, req.status_code))

        # For protecting download interupted, download to
        # tmp_fullname firstly, move tmp_fullname to fullname
        # after download finished
        tmp_fullname = fullname + "_tmp"
        total_size = req.headers.get('content-length')
        with open(tmp_fullname, 'wb') as f:
            if total_size:
                for chunk in tqdm.tqdm(
                        req.iter_content(chunk_size=1024),
                        total=(int(total_size) + 1023) // 1024,
                        unit='KB'):
                    f.write(chunk)
            else:
                for chunk in req.iter_content(chunk_size=1024):
                    if chunk:
                        f.write(chunk)
        shutil.move(tmp_fullname, fullname)

    return fullname

def _md5check(fullname, md5sum=None):
    if md5sum is None:
        return True

    _logger.info("File {} md5 checking...".format(fullname))
    md5 = hashlib.md5()
    with open(fullname, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b""):
            md5.update(chunk)
    calc_md5sum = md5.hexdigest()

    if calc_md5sum != md5sum:
        _logger.info("File {} md5 check failed, {}(calc) != "
                    "{}(base)".format(fullname, calc_md5sum, md5sum))
        return False
    return True

def _decompress(fname):
    """
    Decompress for zip and tar file
    """
    _logger.info("Decompressing {}...".format(fname))

    # For protecting decompressing interupted,
    # decompress to fpath_tmp directory firstly, if decompress
    # successed, move decompress files to fpath and delete
    # fpath_tmp and remove download compress file.
    fpath = os.path.split(fname)[0]
    fpath_tmp = os.path.join(fpath, 'tmp')
    if os.path.isdir(fpath_tmp):
        shutil.rmtree(fpath_tmp)
        os.makedirs(fpath_tmp)

    if fname.find('tar') >= 0:
        with tarfile.open(fname) as tf:
            def is_within_directory(directory, target):
                
                abs_directory = os.path.abspath(directory)
                abs_target = os.path.abspath(target)
            
                prefix = os.path.commonprefix([abs_directory, abs_target])
                
                return prefix == abs_directory
            
            def safe_extract(tar, path=".", members=None, *, numeric_owner=False):
            
                for member in tar.getmembers():
                    member_path = os.path.join(path, member.name)
                    if not is_within_directory(path, member_path):
                        raise Exception("Attempted Path Traversal in Tar File")
            
                tar.extractall(path, members, numeric_owner=numeric_owner) 
                
            
            safe_extract(tf, path=fpath_tmp)
    elif fname.find('zip') >= 0:
        with zipfile.ZipFile(fname) as zf:
            zf.extractall(path=fpath_tmp)
    else:
        raise TypeError("Unsupport compress file type {}".format(fname))

    for f in os.listdir(fpath_tmp):
        src_dir = os.path.join(fpath_tmp, f)
        dst_dir = os.path.join(fpath, f)
        _move_and_merge_tree(src_dir, dst_dir)

    shutil.rmtree(fpath_tmp)
    os.remove(fname)

def _move_and_merge_tree(src, dst):
    """
    Move src directory to dst, if dst is already exists,
    merge src to dst
    """
    if not os.path.exists(dst):
        shutil.move(src, dst)
    else:
        for fp in os.listdir(src):
            src_fp = os.path.join(src, fp)
            dst_fp = os.path.join(dst, fp)
            if os.path.isdir(src_fp):
                if os.path.isdir(dst_fp):
                    _move_and_merge_tree(src_fp, dst_fp)
                else:
                    shutil.move(src_fp, dst_fp)
            elif os.path.isfile(src_fp) and \
                    not os.path.isfile(dst_fp):
                shutil.move(src_fp, dst_fp)
