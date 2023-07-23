import os
import random

import soundfile as sf
import torch
from speechbrain.pretrained import EncoderClassifier
from torch.multiprocessing import Manager
from torch.multiprocessing import Process
from torch.utils.data import Dataset
from torchaudio.transforms import Resample
from tqdm import tqdm

from Preprocessing.CodecAudioPreprocessor import CodecAudioPreprocessor
from Preprocessing.TextFrontend import ArticulatoryCombinedTextFrontend
from Utility.storage_config import MODELS_DIR


class CodecAlignerDataset(Dataset):

    def __init__(self,
                 path_to_transcript_dict,
                 cache_dir,
                 lang,
                 loading_processes,
                 device,
                 min_len_in_seconds=1,
                 max_len_in_seconds=15,
                 rebuild_cache=False,
                 verbose=False,
                 phone_input=False,
                 allow_unknown_symbols=False):
        os.makedirs(cache_dir, exist_ok=True)
        if not os.path.exists(os.path.join(cache_dir, "aligner_train_cache.pt")) or rebuild_cache:
            torch.multiprocessing.set_start_method('spawn', force=True)
            if type(path_to_transcript_dict) != dict:
                path_to_transcript_dict = path_to_transcript_dict()  # in this case we passed a function instead of the dict, so that the function isn't executed if not necessary.
            resource_manager = Manager()
            self.path_to_transcript_dict = resource_manager.dict(path_to_transcript_dict)
            key_list = list(self.path_to_transcript_dict.keys())
            with open(os.path.join(cache_dir, "files_used.txt"), encoding='utf8', mode="w") as files_used_note:
                files_used_note.write(str(key_list))
            fisher_yates_shuffle(key_list)
            # build cache
            print("... building dataset cache ...")
            self.result_pool = resource_manager.list()
            # make processes
            key_splits = list()
            process_list = list()
            for i in range(loading_processes):
                key_splits.append(
                    key_list[i * len(key_list) // loading_processes:(i + 1) * len(key_list) // loading_processes])
            for key_split in key_splits:
                process_list.append(
                    Process(target=self.cache_builder_process,
                            args=(key_split,
                                  lang,
                                  min_len_in_seconds,
                                  max_len_in_seconds,
                                  verbose,
                                  device,
                                  phone_input,
                                  allow_unknown_symbols),
                            daemon=True))
                process_list[-1].start()
            for process in process_list:
                process.join()
            print("pooling results...")
            pooled_datapoints = list()
            for chunk in self.result_pool:
                for datapoint in chunk:
                    pooled_datapoints.append(datapoint)  # unpack into a joint list
            self.result_pool = pooled_datapoints
            del pooled_datapoints
            print("converting text to tensors...")
            text_tensors = [torch.ShortTensor(x[0]) for x in self.result_pool]  # turn everything back to tensors (had to turn it to np arrays to avoid multiprocessing issues)
            print("converting speech to tensors...")
            speech_tensors = [torch.ShortTensor(x[1]) for x in self.result_pool]
            print("converting waves to tensors...")
            norm_waves = [torch.Tensor(x[2]) for x in self.result_pool]
            print("unpacking file list...")
            filepaths = [x[3] for x in self.result_pool]
            del self.result_pool
            self.datapoints = list(zip(text_tensors, speech_tensors))
            del text_tensors
            del speech_tensors
            print("done!")

            # add speaker embeddings
            self.speaker_embeddings = list()
            speaker_embedding_func_ecapa = EncoderClassifier.from_hparams(source="speechbrain/spkrec-ecapa-voxceleb",
                                                                          run_opts={"device": str(device)},
                                                                          savedir=os.path.join(MODELS_DIR, "Embedding", "speechbrain_speaker_embedding_ecapa"))
            with torch.inference_mode():
                for wave in tqdm(norm_waves):
                    self.speaker_embeddings.append(speaker_embedding_func_ecapa.encode_batch(wavs=wave.to(device).unsqueeze(0)).squeeze().cpu())

            # save to cache
            if len(self.datapoints) == 0:
                raise RuntimeError  # something went wrong and there are no datapoints
            torch.save((self.datapoints, None, self.speaker_embeddings, filepaths),
                       os.path.join(cache_dir, "aligner_train_cache.pt"))
        else:
            # just load the datapoints from cache
            self.datapoints = torch.load(os.path.join(cache_dir, "aligner_train_cache.pt"), map_location='cpu')
            self.speaker_embeddings = self.datapoints[2]
            self.datapoints = self.datapoints[0]

        self.tf = ArticulatoryCombinedTextFrontend(language=lang)
        self.ap = None
        print(f"Prepared an Aligner dataset with {len(self.datapoints)} datapoints in {cache_dir}.")

    def cache_builder_process(self,
                              path_list,
                              lang,
                              min_len,
                              max_len,
                              verbose,
                              device,
                              phone_input,
                              allow_unknown_symbols):
        process_internal_dataset_chunk = list()
        tf = ArticulatoryCombinedTextFrontend(language=lang)
        _, sr = sf.read(path_list[0])
        assumed_sr = sr
        ap = CodecAudioPreprocessor(input_sr=assumed_sr, device=device)
        resample = Resample(orig_freq=assumed_sr, new_freq=16000).to(device)

        for path in tqdm(path_list):
            if self.path_to_transcript_dict[path].strip() == "":
                continue

            try:
                wave, sr = sf.read(path)
            except:
                print(f"Problem with an audio file: {path}")
                continue

            if sr != assumed_sr:
                print(f"{path} has a different sampling rate --> skipping")
                continue

            dur_in_seconds = len(wave) / sr
            if not (min_len <= dur_in_seconds <= max_len):
                if verbose:
                    print(f"Excluding {path} because of its duration of {round(dur_in_seconds, 2)} seconds.")
                continue
            try:
                norm_wave = resample(torch.Tensor(wave, device=device, dtype=torch.float))
            except ValueError:
                continue
            dur_in_seconds = len(norm_wave) / 16000
            if not (min_len <= dur_in_seconds <= max_len):
                if verbose:
                    print(f"Excluding {path} because of its duration of {round(dur_in_seconds, 2)} seconds.")
                continue
            # raw audio preprocessing is done
            transcript = self.path_to_transcript_dict[path]

            try:
                try:
                    cached_text = tf.string_to_tensor(transcript, handle_missing=False, input_phonemes=phone_input).squeeze(0).cpu().numpy()
                except KeyError:
                    cached_text = tf.string_to_tensor(transcript, handle_missing=True, input_phonemes=phone_input).squeeze(0).cpu().numpy()
                    if not allow_unknown_symbols:
                        continue  # we skip sentences with unknown symbols
            except ValueError:
                # this can happen for Mandarin Chinese, when the syllabification of pinyin doesn't work. In that case, we just skip the sample.
                continue
            except KeyError:
                # this can happen for Mandarin Chinese, when the syllabification of pinyin doesn't work. In that case, we just skip the sample.
                continue

            cached_speech = ap.audio_to_codebook_indexes(audio=wave, current_sampling_rate=sr).transpose(0, 1).cpu().numpy()
            process_internal_dataset_chunk.append([cached_text,
                                                   cached_speech,
                                                   norm_wave.cpu().detach().numpy(),
                                                   path])
        self.result_pool.append(process_internal_dataset_chunk)

    def __getitem__(self, index):
        if self.ap is None:
            self.ap = CodecAudioPreprocessor(input_sr=-1)  # only used to transform features into continuous matrices
        text_vector = self.datapoints[index][0]
        tokens = self.tf.text_vectors_to_id_sequence(text_vector=text_vector)
        tokens = torch.LongTensor(tokens)
        token_len = torch.LongTensor([len(tokens)])
        speech_indexes = self.datapoints[index][1]
        speech_len = torch.LongTensor([len(speech_indexes)])
        speech = self.ap.indexes_to_codec_frames(speech_indexes.int().transpose(0, 1)).transpose(0, 1).detach()
        return tokens, \
               token_len, \
               speech, \
               speech_len, \
               self.speaker_embeddings[index]

    def __len__(self):
        return len(self.datapoints)


def fisher_yates_shuffle(lst):
    for i in range(len(lst) - 1, 0, -1):
        j = random.randint(0, i)
        lst[i], lst[j] = lst[j], lst[i]