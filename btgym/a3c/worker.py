# This code borrows heavily from OpenAI universal_starter_agent:
# https://github.com/openai/universe-starter-agent
# Under MIT licence.
#
# Paper: https://arxiv.org/abs/1602.01783

import sys
sys.path.insert(0,'..')

import os
import logging
import multiprocessing

import cv2
import tensorflow as tf

from .a3c import A3C
from .envs import create_env

class FastSaver(tf.train.Saver):
    """
    Disables write_meta_graph argument,
    which freezes entire process and is mostly useless.
    """
    def save(self,
             sess,
             save_path,
             global_step=None,
             latest_filename=None,
             meta_graph_suffix="meta",
             write_meta_graph=True):
        super(FastSaver, self).save(sess,
                                    save_path,
                                    global_step,
                                    latest_filename,
                                    meta_graph_suffix,
                                    False)

class Worker(multiprocessing.Process):
    """___"""
    env = None

    def __init__(self,
                 env_class,
                 env_config,
                 policy_class,
                 policy_config,
                 cluster_spec,
                 job_name,
                 task,
                 log_dir,
                 log,
                 log_level,
                 max_steps=1000000000,
                 test_mode=False,
                 **kwargs):
        """___"""
        super(Worker, self).__init__()
        self.env_class = env_class
        self.env_config = env_config
        self.policy_class = policy_class
        self.policy_config = policy_config
        self.cluster_spec = cluster_spec
        self.job_name = job_name
        self.task = task
        self.log_dir = log_dir
        self.max_steps = max_steps
        self.log = log
        logging.basicConfig()
        self.log = logging.getLogger('{}_{}'.format(self.job_name, self.task))
        self.log.setLevel(log_level)
        self.kwargs = kwargs
        self.test_mode = test_mode

    def run(self):
        """
        Worker runtime body.
        """
        tf.reset_default_graph()

        if self.test_mode:
            import gym

        # Define cluster:
        cluster = tf.train.ClusterSpec(self.cluster_spec).as_cluster_def()

        # Start tf.server:
        if self.job_name in 'ps':
            server = tf.train.Server(
                cluster,
                job_name=self.job_name,
                task_index=self.task,
                config=tf.ConfigProto(device_filters=["/job:ps"])
            )
            self.log.debug('parameters_server started.')
            # Just block here:
            server.join()

        else:
            server = tf.train.Server(
                cluster,
                job_name='worker',
                task_index=self.task,
                config=tf.ConfigProto(
                    intra_op_parallelism_threads=1,  # original was: 1
                    inter_op_parallelism_threads=1  # original was: 2
                )
            )
            self.log.debug('worker_{} tf.server started.'.format(self.task))

            self.log.debug('making environment.')
            if not self.test_mode:
                # Assume BTgym env. class:
                self.log.debug('worker_{} is data_master: {}'.format(self.task, self.env_config['data_master']))
                try:
                    self.env = self.env_class(**self.env_config)

                except:
                    raise SystemExit(' Worker_{} failed to make BTgym environment'.format(self.task))

            else:
                # Assume atari testing:
                try:
                    self.env = create_env(self.env_config['gym_id'])

                except:
                    raise SystemExit(' Worker_{} failed to make Atari Gym environment'.format(self.task))

            self.log.debug('worker_{}:envronment ok.'.format(self.task))
            # Define trainer:
            trainer = A3C(
                env=self.env,
                task=self.task,
                policy_class=self.policy_class,
                policy_config=self.policy_config,
                test_mode=self.test_mode,
                log=self.log,
                **self.kwargs)

            self.log.debug('worker_{}:trainer ok.'.format(self.task))

            # Saver-related:
            variables_to_save = [v for v in tf.global_variables() if not v.name.startswith("local")]
            init_op = tf.variables_initializer(variables_to_save)
            init_all_op = tf.global_variables_initializer()

            saver = FastSaver(variables_to_save)

            self.log.debug('worker_{}: vars_to_save:'.format(self.task))
            for v in variables_to_save:
                self.log.debug('{}: {}'.format(v.name, v.get_shape()))

            def init_fn(ses):
                self.log.debug("Initializing all parameters.")
                ses.run(init_all_op)

            config = tf.ConfigProto(device_filters=["/job:ps", "/job:worker/task:{}/cpu:0".format(self.task)])
            logdir = os.path.join(self.log_dir, 'train')
            summary_dir = logdir + "_{}".format(self.task)

            summary_writer = tf.summary.FileWriter(summary_dir)

            sv = tf.train.Supervisor(
                is_chief=(self.task == 0),
                logdir=logdir,
                saver=saver,
                summary_op=None,
                init_op=init_op,
                init_fn=init_fn,
                #summary_writer=summary_writer,
                ready_op=tf.report_uninitialized_variables(variables_to_save),
                global_step=trainer.global_step,
                save_model_secs=300,
            )
            self.log.debug("worker_{}: connecting to the parameter server... ".format(self.task))

            with sv.managed_session(server.target, config=config) as sess, sess.as_default():

                sess.run(trainer.sync)
                trainer.start(sess, summary_writer)
                global_step = sess.run(trainer.global_step)
                self.log.warning("worker_{}: starting training at step: {}".format(self.task, global_step))
                while not sv.should_stop() and global_step < self.max_steps:
                    trainer.process(sess)
                    global_step = sess.run(trainer.global_step)

                # Ask for all the services to stop:
                self.env.close()
                sv.stop()
            self.log.warning('worker_{}: reached {} steps, exiting.'.format(self.task, global_step))


class TestTrainer():
    """Dummy trainer class."""
    global_step = 0

    def __init__(self, worker_id):
        self.worker_id = worker_id

    def start(self):
        print('Trainer_{} started.'.format(self.worker_id))

    def sync(self):
        print('Trainer_{}: sync`ed.'.format(self.worker_id))

    def process(self):
        print('Traner_{}: processed step {}'.format(self.worker_id, self.global_step))
        self.global_step += 1

