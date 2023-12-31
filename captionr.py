import argparse
import pathlib
import logging
from dataclasses import dataclass
import sys

from PIL import Image
import os
from captionr.blip_cap import BLIP
from captionr.blip2_cap import BLIP2
from captionr.clip_interrogator import Interrogator, Config
from captionr.coca_cap import Coca
from captionr.git_cap import Git
from captionr.captionr_class import CaptionrConfig, Captionr
from captionr.flamingo_cap import Flamingo
import tqdm

from tqdm.contrib.concurrent import process_map  # or thread_map
from multiprocessing import set_start_method

class TqdmLoggingHandler(logging.Handler):
    def __init__(self, level=logging.DEBUG):
        super().__init__(level)

    def emit(self, record):
        try:
            msg = self.format(record)
            tqdm.tqdm.write(msg)
            self.flush()
        except Exception:
            self.handleError(record) 

config:CaptionrConfig = None

def init_argparse() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
                        prog = 'Captionr',
                        usage="%(prog)s [OPTIONS] [FOLDER]...",
                        description="Caption a set of images"
                        )
    parser.add_argument(
                        "-v", "--version", action="version",
                        version = f"{parser.prog} version 0.0.1"
                        )
    parser.add_argument('folder', 
                        help='One or more folders to scan for iamges. Images should be jpg/png.',
                        type=pathlib.Path,
                        nargs='*',
                        )
    parser.add_argument('--output', 
                        help='Output to a folder rather than side by side with image files',
                        type=pathlib.Path,
                        nargs=1
                        )
    parser.add_argument('--existing',
                        help='Action to take for existing caption files (default: skip)',
                        choices=['skip','ignore','copy','prepend','append', 'flavor'],
                        default='skip'
                        )
    parser.add_argument('--cap_length',
                        help='Maximum length of caption. (default: 0)',
                        default=0,
                        type=int
                        )
    parser.add_argument('--git_pass',
                        help='Perform a GIT model pass',
                        action='store_true',
                        )
    parser.add_argument('--coca_pass',
                        help='Perform a Coca model pass',
                        action='store_true',
                        )
    parser.add_argument('--blip_pass',
                        help='Perform a BLIP model pass',
                        action='store_true',
                        )
    parser.add_argument('--model_order',
                        help='Perform captioning/fallback using this order (default: coca,git,blip)',
                        default='coca,git,blip',
                        )
    parser.add_argument('--use_blip2',
                        help='Uses BLIP2 for BLIP pass. Only activated when --blip_pass also specified',
                        action='store_true')
    parser.add_argument('--blip2_model',
                        help='Specify the BLIP2 model to use',
                        choices=['Salesforce/blip2-opt-6.7b-coco','Salesforce/blip2-opt-2.7b-coco', 'Salesforce/blip2-opt-2.7b', 'Salesforce/blip2-opt-6.7b', 'Salesforce/blip2-flan-t5-xl-coco', 'ethzanalytics/blip2-flan-t5-xl-sharded', 'Salesforce/blip2-flan-t5-xxl','Salesforce/blip2-flan-t5-xl'],
                        default='Salesforce/blip2-opt-6.7b-coco'
                        )
    parser.add_argument('--blip2_question_file',
                        help='Specify a question file to use to query BLIP2 and add answers as tags',
                        type=pathlib.Path
                        )
    parser.add_argument('--blip_beams',
                        help='Number of BLIP beams (default: 64)',
                        default=64,
                        type=int
                        )
    parser.add_argument('--blip_min',
                        help='BLIP min length (default: 30)',
                        default=30,
                        type=int
                        )
    parser.add_argument('--blip_max',
                        help='BLIP max length (default: 75)',
                        default=75,
                        type=int
                        )
    parser.add_argument('--clip_model_name',
                        help='CLIP model to use. Use ViT-H for SD 2.x, ViT-L for SD 1.5 (default: ViT-H-14/laion2b_s32b_b79k)',
                        default='ViT-H-14/laion2b_s32b_b79k',
                        choices=['ViT-H-14/laion2b_s32b_b79k','ViT-L-14/openai','ViT-bigG-14/laion2b_s39b_b160k']
                        )
    parser.add_argument('--clip_flavor',
                        help='Add CLIP Flavors',
                        action='store_true'
                        )
    parser.add_argument('--clip_max_flavors',
                        help='Max CLIP Flavors (default: 8)',
                        default=8,
                        type=int
                        )
    parser.add_argument('--clip_artist',
                        help='Add CLIP Artists',
                        action='store_true'
                        )
    parser.add_argument('--clip_medium',
                        help='Add CLIP Mediums',
                        action='store_true'
                        )
    parser.add_argument('--clip_movement',
                        help='Add CLIP Movements',
                        action='store_true'
                        )
    parser.add_argument('--clip_trending',
                        help='Add CLIP Trendings',
                        action='store_true'
                        )
    parser.add_argument('--clip_method',
                        help='CLIP method to use',
                        choices=['interrogate','interrogate_fast','interrogate_classic'],
                        default='interrogate_fast'
                        )
    parser.add_argument('--fail_phrases',
                        help='Phrases that will fail a caption pass and move to the fallback model. (default: "a sign that says,writing that says,that says,with the word")',
                        default='a sign that says,writing that says,that says,with the word'
                        )
    parser.add_argument('--ignore_tags',
                        help='Comma separated list of tags to ignore',
                        )
    parser.add_argument('--find',
                        help='Perform find and replace with --replace REPLACE',
                        )
    parser.add_argument('--replace',
                        help='Perform find and replace with --find FIND',
                        )
    parser.add_argument('--folder_tag',
                        help='Tag the image with folder name',
                        action='store_true'
                        )
    parser.add_argument('--folder_tag_levels',
                        help='Number of folder levels to tag. (default: 1)',
                        type=int,
                        default=1,
                        )
    parser.add_argument('--folder_tag_stop',
                        help='Do not tag folders any deeper than this path. Overrides --folder_tag_levels if --folder_tag_stop is shallower',
                        type=pathlib.Path,
                        )
    parser.add_argument('--folder_tag_position',
                        help='What positino to insert folder tags into the tag list. (default: 1)',
                        type=int,
                        default=1,
                        )
    parser.add_argument('--uniquify_tags',
                        help='Ensure tags are unique',
                        action='store_true'
                        )
    parser.add_argument('--fuzz_ratio',
                        help='Sets the similarity ratio allowed for tags when uniquifying. If a tag is more than --fuzz_ratio similar to another tag, it will be eliminated. (default: 60.0)',
                        type=float,
                        default=60.0
                        )
    parser.add_argument('--prepend_text',
                        help='Prepend text to final caption',
                        )
    parser.add_argument('--append_text',
                        help='Append text to final caption',
                        )
    parser.add_argument('--preview',
                        help='Do not write to caption file. Just displays preview in STDOUT',
                        action='store_true'
                        )
    parser.add_argument('--use_filename',
                        help='Read the existing caption from the filename, stripping all special characters/numbers',
                        action='store_true'
                        )
    parser.add_argument('--device',
                        help='Device to use. (default: cuda)',
                        choices=['cuda','cpu'],
                        default='cuda'
                        )
    parser.add_argument('--extension',
                        help='Caption file extension. (default: txt)',
                        choices=['txt','caption'],
                        default='txt'
                        )
    parser.add_argument('--num_workers',
                        help='Number of processes to spawn. Lower to reduce VRAM issues. (default: 8)',
                        default=8,
                        type=int
                        )
    parser.add_argument('--quiet',
                        action='store_true'
                        )
    parser.add_argument('--debug',
                        action='store_true'
                        )
    parser.add_argument('--flamingo_pass',
                        help='Perform a Flamingo model pass',
                        action='store_true',
                        )
    parser.add_argument("--flamingo_model", type=str, default="openflamingo/OpenFlamingo-9B-vitl-mpt7b", help="Flamingo model name or path")
    parser.add_argument("--force_cpu", action="store_true", default=False, help="force using CPU even if GPU is available")
    parser.add_argument("--min_new_tokens", type=int, default=20, help="minimum number of tokens to generate")
    parser.add_argument("--max_new_tokens", type=int, default=50, help="maximum number of tokens to generate")
    parser.add_argument("--num_beams", type=int, default=3, help="number of beams, more is more accurate but slower")
    parser.add_argument("--prompt", type=str, default="Output: ", help="prompt to use for generation, default is 'Output: '")
    parser.add_argument("--temperature", type=float, default=1.0, help="temperature for sampling, 1.0 is default")
    parser.add_argument("--top_k", type=int, default=0, help="top_k sampling, 0 is default")
    parser.add_argument("--top_p", type=float, default=0.9, help="top_p sampling, 0.9 is default")
    parser.add_argument("--repetition_penalty", type=float, default=1.0, help="repetition penalty, 1.0 is default")
    parser.add_argument("--length_penalty", type=float, default=1.0, help="length penalty, 1.0 is default")
    parser.add_argument("--example_root", type=pathlib.Path, default="examples", help="Path to 2-3 precaptioned images to guide generation") 
    return parser

def main() -> None:
    global config
    parser = init_argparse()
    config = parser.parse_args()
    config.base_path = os.path.dirname(os.path.abspath(__file__))

    if config.debug:
        logging.basicConfig(level=logging.DEBUG)
        logging.debug(config)
    elif config.quiet:
        logging.basicConfig(level=logging.ERROR)
    else:
        logging.basicConfig(level=logging.INFO)


    log = logging.getLogger(__name__)
    log.addHandler(TqdmLoggingHandler())

    if len(config.folder) == 0:
        parser.error('Folder is required.')

    if config.use_blip2 and config.blip2_question_file is not None:
        if not config.blip2_question_file.exists or not config.blip2_question_file.is_file:
            parser.error("Question file does not exist")
        
        questions = []
        with open(config.blip2_question_file) as file:
            for line in file:
                questions.append(line)

        config.blip2_questions = questions
        
    if not config.git_pass \
            and not config.blip_pass \
            and not config.coca_pass \
            and not config.clip_flavor \
            and not config.clip_artist \
            and not config.clip_medium \
            and not config.clip_movement \
            and not config.clip_trending \
            and not config.flamingo_pass:
        if config.existing == 'skip' \
            and  ( \
                ( \
                    config.find is not None and config.find != '' \
                    and config.replace is not None and config.replace != '' \
                ) \
                or config.folder_tag \
                or ( \
                    config.prepend_text is not None \
                    and config.prepend_text != '' \
                ) \
                or ( \
                    config.append_text is not None \
                    and config.append_text != '' \
                ) \
            ):
            parser.error('--existing=skip cannot be used for find/replace, folder tagging, text prepending/appending unless a caption model is selected. To run a caption pass without a model selected, please choose a different option for existing caption.')
        else:
            if config.existing == 'skip' \
                and not ( \
                    (config.find is not None and config.find != '' \
                    and config.replace is not None and config.replace != '') \
                    or config.folder_tag \
                    or (config.prepend_text is not None \
                        and config.prepend_text != '') \
                    or (config.append_text is not None \
                        and config.append_text != '') \
                ):
            
                parser.error('No captioning flags specified. Use --git_pass | --coca_pass | --blip_pass | --clip_flavor | --clip_artist | --clip_medium | --clip_movement | --clip_trending | --find/--replace | --folder_tag | --prepend_text | --append_text to initate captioning')

    if config.coca_pass:
        logging.info("Loading Coca Model...")
        config._coca = Coca(config.device,max_length=config.cap_length)
    
    if config.git_pass:
        logging.info("Loading Git Model...")
        config._git = Git(config.device,max_length=config.cap_length)

    if config.blip_pass:
        if config.use_blip2:
            logging.info("Loading BLIP Model...")
            config._blip = BLIP2(config.device,model_name=config.blip2_model,max_length=config.cap_length)
        else:
            logging.info("Loading BLIP Model...")
            config._blip = BLIP(config.device,beams=config.blip_beams,blip_max=config.blip_max, blip_min=config.blip_min)


    if config.clip_artist or config.clip_flavor or config.clip_medium or config.clip_movement or config.clip_trending:
        logging.info("Loading Clip Model...")
        config._clip = Interrogator(Config(clip_model_name=config.clip_model_name,
                                           captionr_config=config,
                                           quiet=config.quiet,
                                           data_path=os.path.join(config.base_path,'data'),
                                           cache_path=os.path.join(config.base_path,'data')))
        
    if config.flamingo_pass:
        logging.info("Loading Flamingo Model...")
        config._flamingo = Flamingo(config.device, config.flamingo_model, config.force_cpu, config.example_root)
        
    if config.preview:
        logging.info('PREVIEW MODE ENABLED. No caption files will be written.')
    paths = []
    cptr = Captionr(config=config)
    for folder in config.folder:
        for root, dirs, files in os.walk(folder.absolute(), topdown=False):
            for name in files:
                
                if os.path.splitext(os.path.split(name)[1])[1].upper() not in ['.JPEG','.JPG','.JPE', '.PNG', '.WEBP']:
                    continue
                if config.extension not in os.path.splitext(os.path.split(name)[1])[1]:
                    cap_file = os.path.join(folder.absolute(),os.path.splitext(os.path.split(name)[1])[0] + f'.{config.extension}')
                if not config.existing == 'skip' or not os.path.exists(cap_file):
                    paths.append(os.path.join(root, name))
                elif not config.quiet:
                    logging.info(f'Caption file {cap_file} exists. Skipping.')
    
    def calc_chunksize(n_workers, len_iterable, factor=4):
        chunksize, extra = divmod(len_iterable, n_workers * factor)
        if extra:
            chunksize += 1
        return chunksize
    
    #process_map(cptr.process_img, paths, max_workers=config.num_workers,chunksize=calc_chunksize(config.num_workers,len(paths)))

    for path in tqdm.tqdm(paths):
        cptr.process_img(path)

if __name__ == "__main__":
    #set_start_method('spawn')
    main()
