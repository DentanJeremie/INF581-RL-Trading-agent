import time
from os.path import join

import tensorflow as tf
import numpy as np
from argparse import ArgumentParser

from config import *

from process.processor import Processor

from model.agent import Agent
from model.environment import Environment

from utils.config import get_config
from utils.constants import *
from utils.strings import *
from utils.util import *

config_parser = get_config_parser(config_file_path)
config = get_config(config_parser)
logger = get_logger(config)
with tf.Session() as sess:
    processor = Processor(config, logger)
    env = Environment(logger, config, processor.diff_blocks, processor.price_blocks, processor.timestamp_blocks)
    agent = Agent(sess, logger, config, env)
    agent.train()
    agent.summary_writer.close()


