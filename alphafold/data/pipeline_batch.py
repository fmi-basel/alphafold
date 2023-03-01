# Copyright 2021 DeepMind Technologies Limited
# Copyright 2022 Friedrich Miescher Institute for Biomedical Research
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Modified by Georg Kempf, Friedrich Miescher Institute for Biomedical Research

"""Functions for building the features for the AlphaFold multimer model."""

import collections
import contextlib
import copy
import dataclasses
import json
import os
import tempfile
from typing import Mapping, MutableMapping, Sequence

from absl import logging
from alphafold.common import protein
from alphafold.common import residue_constants
from alphafold.data import feature_processing
from alphafold.data import msa_pairing
from alphafold.data import parsers
from alphafold.data import pipeline
from alphafold.data.pipeline import run_msa_tool
from alphafold.data.tools import jackhmmer
import numpy as np
import signal
from shutil import copyfile

def _make_desc_map(*,
                       sequences: Sequence[str],
                       descriptions: Sequence[str],
                       ) -> Mapping[str, str]:
    """Makes a mapping from description to sequence."""
    if len(sequences) != len(descriptions):
        raise ValueError('sequences and descriptions must have equal length. '
                         f'Got {len(sequences)} != {len(descriptions)}.')
    desc_map = {}
    for sequence, description in zip(sequences, descriptions):
        desc_map[description] = sequence
    return desc_map


@contextlib.contextmanager
def temp_fasta_file(fasta_str: str, custom_tempdir: str):
    if custom_tempdir is None:
        dir = "/tmp"
    else:
        dir = custom_tempdir
    with tempfile.NamedTemporaryFile('w', suffix='.fasta', dir=dir) as fasta_file:
        fasta_file.write(fasta_str)
        fasta_file.seek(0)
        yield fasta_file.name

class DataPipeline:
    """Runs the alignment tools and assembles the input features."""

    def __init__(self,
                monomer_data_pipeline: pipeline.DataPipeline, 
                jackhmmer_binary_path: str,
                uniprot_database_path: str,
                max_uniprot_hits: int = 50000):
        """Initializes the data pipeline.

        Args:
          monomer_data_pipeline: An instance of pipeline.DataPipeline - that runs
            the data pipeline for the monomer AlphaFold system.
        """
        self._monomer_data_pipeline = monomer_data_pipeline
        self.use_precomputed_msas = self._monomer_data_pipeline.use_precomputed_msas
        self.custom_tempdir = self._monomer_data_pipeline.custom_tempdir
        self.precomputed_msas_path = self._monomer_data_pipeline.precomputed_msas_path

        self._uniprot_msa_runner = jackhmmer.Jackhmmer(
            binary_path=jackhmmer_binary_path,
            database_path=uniprot_database_path,
            custom_tempdir=self.custom_tempdir)

    def _process_single_chain(
            self,
            sequence: str,
            description: str,
            msa_output_dir: str,
            no_msa: bool,
            no_template: bool,
            custom_template: str,
            precomputed_msas: str,
            num_cpu: int) -> pipeline.FeatureDict:
        """Runs the monomer pipeline on a single chain."""
        fasta_str = f'>{description}\n{sequence}\n'
        msa_output_dir = os.path.join(msa_output_dir, description)
        if not os.path.exists(msa_output_dir):
            os.makedirs(msa_output_dir)
        with temp_fasta_file(fasta_str, self.custom_tempdir) as fasta_path:
            logging.info('Running monomer pipeline on %s',
                         description)
            features = self._monomer_data_pipeline.process(
                input_fasta_path=fasta_path,
                msa_output_dir=msa_output_dir,
                no_msa=no_msa,
                no_template=no_template,
                custom_template=custom_template,
                precomputed_msas=precomputed_msas,
                num_cpu=num_cpu)

            #Also run uniprot search for multimer
            out_path = os.path.join(msa_output_dir, 'uniprot_hits.sto')
            out_path_a3m = os.path.join(msa_output_dir, 'uniprot_hits.a3m')
            self._uniprot_msa_runner.n_cpu = num_cpu
            if not os.path.exists(out_path) and not os.path.exists(out_path_a3m):
                result = run_msa_tool(
                    self._uniprot_msa_runner, fasta_path, out_path, 'sto',
                    self._monomer_data_pipeline.use_precomputed_msas)
                with open(out_path, 'w') as f:
                    f.write(result['sto'])

        return features

    def process(self,
                input_fasta_path: str,
                msa_output_dir: str,
                no_msa,
                no_template,
                custom_template,
                precomputed_msas,
                num_cpu) -> pipeline.FeatureDict:
        """Runs alignment tools on the input sequences and creates features."""
        with open(input_fasta_path) as f:
            input_fasta_str = f.read()
        input_seqs, input_descs = parsers.parse_fasta(input_fasta_str)
        desc_map = _make_desc_map(sequences=input_seqs,
                                    descriptions=input_descs)

        desc_map_path = os.path.join(msa_output_dir, 'desc_map.json')
        with open(desc_map_path, 'w') as f:
            json.dump(desc_map, f, indent=4, sort_keys=True)

        all_features = []
        for i, (desc, sequence) in enumerate(desc_map.items()):
            features = self._process_single_chain(
                sequence=sequence,
                description=desc,
                msa_output_dir=msa_output_dir,
                no_msa=no_msa[i],
                no_template=no_template[i],
                custom_template=custom_template[i],
                precomputed_msas=precomputed_msas[i],
                num_cpu=num_cpu)

            all_features.append(features)

        return all_features