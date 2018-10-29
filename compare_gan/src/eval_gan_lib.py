# coding=utf-8
# Copyright 2018 Google LLC & Hwalsuk Lee.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Evaluation for GAN tasks."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import abc
import csv
import os

from compare_gan.src import fid_score as fid_score_lib
from compare_gan.src import gan_lib
from compare_gan.src import gilbo as gilbo_lib
from compare_gan.src import jacobian_conditioning as conditioning_lib
from compare_gan.src import kid_score as kid_score_lib
from compare_gan.src import ms_ssim_score
from compare_gan.src import params
from compare_gan.src.gans import consts

import numpy as np
import pandas as pd
import scipy.spatial
from six.moves import range
import tensorflow as tf

flags = tf.flags
logging = tf.logging
FLAGS = flags.FLAGS

# This stores statistics for the real data and should be computed only
# once for the whole execution.
MU_REAL, SIGMA_REAL = None, None

# Special value returned when fake image generated by GAN has nans.
NAN_DETECTED = 31337.0

# Special value returned when FID code returned exception.
FID_CODE_FAILED = 4242.0

# If the given param was not specified in the model, use this default.
# This is mostly for COLAB, which tries to automatically infer the type
# of the column
DEFAULT_VALUES = {
    "weight_clipping": -1.0,
    "y_dim": -1,
    "lambda": -1.0,
    "disc_iters": -1,
    "beta1": -1.0,
    "beta2": -1.0,
    "gamma": -1.0,
    "penalty_type": "none",
    "discriminator_spectralnorm": False,
    "architecture": "unknown",
}

# Inception batch size.
INCEPTION_BATCH = 50

# List of models that we consider.
SUPPORTED_GANS = [
    "GAN", "GAN_MINMAX", "WGAN", "WGAN_GP", "DRAGAN", "LSGAN", "VAE", "BEGAN"
] + consts.MODELS_WITH_PENALTIES


def GetAllTrainingParams():
  all_params = set(["architecture"])
  for gan_type in SUPPORTED_GANS:
    for _ in ["mnist", "fashion-mnist", "cifar10", "celeba"]:
      p = params.GetParameters(gan_type, "wide")
      all_params.update(p.keys())
  logging.info("All training parameter exported: %s", sorted(all_params))
  return sorted(all_params)


# Fake images are already re-scaled to [0, 255] range.
def GetInceptionScore(fake_images, inception_graph):
  """Compute the Inception score."""
  assert fake_images.shape[3] == 3
  num_images = fake_images.shape[0]
  assert num_images % INCEPTION_BATCH == 0

  with tf.Graph().as_default():
    images = tf.constant(fake_images)
    inception_score_op = fid_score_lib.inception_score_fn(
        images,
        num_batches=num_images // INCEPTION_BATCH,
        inception_graph=inception_graph)
    with tf.train.MonitoredSession() as sess:
      inception_score = sess.run(inception_score_op)
      return inception_score


# Images must have the same resolution and pixels must be in 0..255 range.
def ComputeKIDScore(fake_images, real_images, inception_graph):
  """Compute KID score using the kid_score library."""
  assert fake_images.shape[3] == 3
  assert real_images.shape[3] == 3
  bs_real = real_images.shape[0]
  bs_fake = fake_images.shape[0]
  assert bs_real % INCEPTION_BATCH == 0
  assert bs_fake % INCEPTION_BATCH == 0
  assert bs_real >= bs_fake and bs_real % bs_fake == 0
  ratio = bs_real // bs_fake
  logging.info("Ratio of real/fake images is: %d", ratio)
  with tf.Graph().as_default():
    fake_images_batch = tf.train.batch(
        [tf.convert_to_tensor(fake_images, dtype=tf.float32)],
        enqueue_many=True,
        batch_size=INCEPTION_BATCH)
    real_images_batch = tf.train.batch(
        [tf.convert_to_tensor(real_images, dtype=tf.float32)],
        enqueue_many=True,
        batch_size=INCEPTION_BATCH * ratio)
    eval_fn = kid_score_lib.get_kid_function(
        gen_image_tensor=fake_images_batch,
        real_image_tensor=real_images_batch,
        num_gen_images=fake_images.shape[0],
        num_eval_images=real_images.shape[0],
        image_range="0_255",
        inception_graph=inception_graph)
    with tf.train.MonitoredTrainingSession() as sess:
      kid_score = eval_fn(sess)
  return kid_score


# Images must have the same resolution and pixels must be in 0..255 range.
def ComputeTFGanFIDScore(fake_images, real_images, inception_graph):
  """Compute FID score using TF.Gan library."""
  assert fake_images.shape[3] == 3
  assert real_images.shape[3] == 3
  bs_real = real_images.shape[0]
  bs_fake = fake_images.shape[0]
  assert bs_real % INCEPTION_BATCH == 0
  assert bs_fake % INCEPTION_BATCH == 0
  assert bs_real >= bs_fake and bs_real % bs_fake == 0
  ratio = bs_real // bs_fake
  logging.info("Ratio of real/fake images is: %d", ratio)
  with tf.Graph().as_default():
    fake_images_batch = tf.train.batch(
        [tf.convert_to_tensor(fake_images, dtype=tf.float32)],
        enqueue_many=True,
        batch_size=INCEPTION_BATCH)
    real_images_batch = tf.train.batch(
        [tf.convert_to_tensor(real_images, dtype=tf.float32)],
        enqueue_many=True,
        batch_size=INCEPTION_BATCH * ratio)
    eval_fn = fid_score_lib.get_fid_function(
        gen_image_tensor=fake_images_batch,
        real_image_tensor=real_images_batch,
        num_gen_images=fake_images.shape[0],
        num_eval_images=real_images.shape[0],
        image_range="0_255",
        inception_graph=inception_graph)
    with tf.train.MonitoredTrainingSession() as sess:
      fid_score = eval_fn(sess)
  return fid_score


def GetRealImages(dataset,
                  split_name,
                  num_examples,
                  failure_on_insufficient_examples=True):
  """Get num_examples images from the given dataset/split."""
  # Multithread and buffer could improve the training speed by 20%, however it
  # consumes more memory. In evaluation, we used single thread without buffer
  # to avoid using too much memory.
  dataset_content = gan_lib.load_dataset(
      dataset,
      split_name=split_name,
      num_threads=1,
      buffer_size=0)
  # Get real images from the dataset. In the case of a 1-channel
  # dataset (like mnist) convert it to 3 channels.
  data_x = []
  with tf.Graph().as_default():
    get_next = dataset_content.make_one_shot_iterator().get_next()
    with tf.train.MonitoredTrainingSession() as sess:
      for i in range(num_examples):
        try:
          data_x.append(sess.run(get_next[0]))
        except tf.errors.OutOfRangeError:
          logging.error("Reached the end of dataset. Read: %d samples." % i)
          break

  real_images = np.array(data_x)
  if real_images.shape[0] != num_examples:
    if failure_on_insufficient_examples:
      raise ValueError("Not enough examples in the dataset %s: %d / %d" %
                       (dataset, real_images.shape[0], num_examples))
    else:
      logging.error("Not enough examples in the dataset %s: %d / %d", dataset,
                    real_images.shape[0], num_examples)

  real_images *= 255.0
  return real_images


def ShouldRunAccuracyLossTrainVsTest(options):
  """Only run the accuracy test for the NS GAN."""
  return options["gan_type"] == consts.GAN_WITH_PENALTY


def ComputeAccuracyLoss(options,
                        sess,
                        gan,
                        test_images,
                        max_train_examples=50000,
                        num_repeat=5):
  """Compute discriminator accurate/loss on train/test/fake set.

  Args:
    options: Dict[Text, Text] with all parameters for the current trial.
    sess: Tf.Session object.
    gan: Any AbstractGAN instance.
    test_images: numpy array with test images.
    max_train_examples: How many "train" examples to get from the dataset.
                        In each round, some of them will be randomly selected
                        to evaluate train set accuracy.
    num_repeat: How many times to repreat the computation.
                The mean of all the results is reported.
  Returns:
    Dict[Text, float] with all the computed scores.

  Raises:
    ValueError: If the number of test_images is greater than the number of
                training images returned by the dataset.
  """
  train_images = GetRealImages(
      options["dataset"],
      split_name="train",
      num_examples=max_train_examples,
      failure_on_insufficient_examples=False)
  if train_images.shape[0] < test_images.shape[0]:
    raise ValueError("num_train %d must be larger than num_test %d." %
                     (train_images.shape[0], test_images.shape[0]))

  logging.info("Evaluating training and test accuracy...")

  num_batches = int(np.floor(test_images.shape[0] / gan.batch_size))
  if num_batches * gan.batch_size < test_images.shape[0]:
    logging.error("Ignoring the last batch with %d samples / %d epoch size.",
                  test_images.shape[0] - num_batches * gan.batch_size,
                  gan.batch_size)

  train_accuracies = []
  test_accuracies = []
  fake_accuracies = []
  train_d_losses = []
  test_d_losses = []
  for repeat in range(num_repeat):
    idx = np.random.choice(train_images.shape[0], test_images.shape[0])
    train_subset = [train_images[i] for i in idx]

    train_predictions = []
    test_predictions = []
    fake_predictions = []
    train_d_losses = []
    test_d_losses = []

    for i in range(num_batches):
      z_sample = gan.z_generator(gan.batch_size, gan.z_dim)
      start_idx = i * gan.batch_size
      end_idx = start_idx + gan.batch_size
      test_batch = test_images[start_idx:end_idx]
      train_batch = train_subset[start_idx:end_idx]

      test_prediction, test_d_loss, fake_images = sess.run(
          [gan.discriminator_output, gan.d_loss, gan.fake_images],
          feed_dict={
              gan.inputs: test_batch,
              gan.z: z_sample
          })
      test_predictions.append(test_prediction[0])
      test_d_losses.append(test_d_loss)

      train_prediction, train_d_loss = sess.run(
          [gan.discriminator_output, gan.d_loss],
          feed_dict={
              gan.inputs: train_batch,
              gan.z: z_sample
          })
      train_predictions.append(train_prediction[0])
      train_d_losses.append(train_d_loss)

      fake_prediction = sess.run(
          gan.discriminator_output, feed_dict={gan.inputs: fake_images})[0]
      fake_predictions.append(fake_prediction)

    discriminator_threshold = 0.5
    train_predictions = [
        x >= discriminator_threshold for x in train_predictions
    ]
    test_predictions = [x >= discriminator_threshold for x in test_predictions]
    fake_predictions = [x < discriminator_threshold for x in fake_predictions]

    train_accuracy = sum(train_predictions) / float(len(train_predictions))
    test_accuracy = sum(test_predictions) / float(len(test_predictions))
    fake_accuracy = sum(fake_predictions) / float(len(fake_predictions))
    train_d_loss = np.mean(train_d_losses)
    test_d_loss = np.mean(test_d_losses)
    print("repeat %d: train_accuracy: %.3f, test_accuracy: %.3f, "
          "fake_accuracy: %.3f, train_d_loss: %.3f, test_d_loss: %.3f" %
          (repeat, train_accuracy, test_accuracy, fake_accuracy, train_d_loss,
           test_d_loss))

    train_accuracies.append(train_accuracy)
    test_accuracies.append(test_accuracy)
    fake_accuracies.append(fake_accuracy)
    train_d_losses.append(train_d_loss)
    test_d_losses.append(test_d_loss)

  result_dict = {}
  result_dict["train_accuracy"] = np.mean(train_accuracies)
  result_dict["test_accuracy"] = np.mean(test_accuracies)
  result_dict["fake_accuracy"] = np.mean(fake_accuracies)
  result_dict["train_d_loss"] = np.mean(train_d_losses)
  result_dict["test_d_loss"] = np.mean(test_d_losses)
  return result_dict


def ShouldRunMultiscaleSSIM(options):
  msssim_datasets = ["celeba", "celebahq128"]
  return options["dataset"] in msssim_datasets


def ComputeMultiscaleSSIMScore(fake_images):
  """Compute ms-ssim score ."""
  batch_size = 64
  with tf.Graph().as_default():
    fake_images_batch = tf.train.shuffle_batch(
        [tf.convert_to_tensor(fake_images, dtype=tf.float32)],
        capacity=16*batch_size,
        min_after_dequeue=8*batch_size,
        num_threads=4,
        enqueue_many=True,
        batch_size=batch_size)

    # Following section 5.3 of https://arxiv.org/pdf/1710.08446.pdf, we only
    # evaluate 5 batches of the generated images.
    eval_fn = ms_ssim_score.get_metric_function(
        generated_images=fake_images_batch, num_batches=5)
    with tf.train.MonitoredTrainingSession() as sess:
      score = eval_fn(sess)
  return score


class EvalTask(object):
  """Class that describes a single evaluation task.

  For example: compute inception score or compute accuracy.
  The classes that inherit from it, should implement the methods below.
  """
  __metaclass__ = abc.ABCMeta

  @abc.abstractmethod
  def MetricsList(self):
    """List of metrics that this class generates.

    These are the only keys that RunXX methods can return in
    their output maps.
    Returns:
      frozenset of strings, which are the names of the metrics that task
      computes.
    """
    return frozenset()

  def RunInSession(self, options, sess, gan, real_images):
    """Runs the task inside the session, which allows access to tf Graph.

    This code is run after all images were generated, but the session
    is still active. It allows looking into the contents of the graph.

    WARNING: real_images might have 1 or 3 channels (depending on the dataset).

    Args:
      options: Dict, containing all parameters for the current trial.
      sess: tf.Session object.
      gan: AbstractGAN object, that is already present in the current tf.Graph.
      real_images: numpy array with real 'train' images.

    Returns:
      Dict with metric values. The keys must be contained in the set that
      "MetricList" method above returns.
    """
    del options, sess, gan, real_images
    return {}

  def RunAfterSession(self, options, fake_images, real_images):
    """Runs the task after all the generator calls, after session was closed.

    WARNING: the images here, are in 0..255 range, with 3 color channels.
    Args:
      options: Dict, containing all parameters for the current trial.
      fake_images: numpy array with generated images. Expanded 3 channels,
        values 0..255. dtype: float.
      real_images: numpy array with real 'train' images, they are expanded 3
        channels. dtype: float

    Returns:
      Dict with metric values. The keys must be contained in the set that
      "MetricList" method above returns.
    """
    del options, fake_images, real_images
    return {}


class InceptionScoreTask(EvalTask):
  """Task that computes inception score for the generated images."""

  def __init__(self, inception_graph):
    self._inception_graph = inception_graph

  _INCEPTION_SCORE = "inception_score"

  def MetricsList(self):
    return frozenset([self._INCEPTION_SCORE])

  # 'RunInSession' it not needed for this task.

  def RunAfterSession(self, options, fake_images, real_images):
    del options, real_images
    logging.info("Computing inception score.")
    result_dict = {}
    result_dict[self._INCEPTION_SCORE] = GetInceptionScore(
        fake_images, self._inception_graph)
    logging.info("Inception score computed: %.3f",
                 result_dict[self._INCEPTION_SCORE])
    return result_dict


class FIDScoreTask(EvalTask):
  """Task that computes FID score for the generated images."""

  def __init__(self, inception_graph):
    self._inception_graph = inception_graph

  _FID_SCORE = "fid_score"

  def MetricsList(self):
    return frozenset([self._FID_SCORE])

  # 'RunInSession' it not needed for this task.

  def RunAfterSession(self, options, fake_images, real_images):
    del options
    logging.info("Computing FID score.")
    result_dict = {}
    result_dict[self._FID_SCORE] = ComputeTFGanFIDScore(
        fake_images, real_images, self._inception_graph)
    logging.info("Frechet Inception Distance is %.3f",
                 result_dict[self._FID_SCORE])
    return result_dict


class KIDScoreTask(EvalTask):
  """Task that computes KID score for the generated images."""

  def __init__(self, inception_graph):
    self._inception_graph = inception_graph

  _KID_SCORE = "kid_score"

  def MetricsList(self):
    return frozenset([self._KID_SCORE])

  # 'RunInSession' it not needed for this task.

  def RunAfterSession(self, options, fake_images, real_images):
    result_dict = {}
    if options.get("compute_kid_score", False):
      logging.info("Computing KID score.")
      result_dict[self._KID_SCORE] = ComputeKIDScore(fake_images, real_images,
                                                     self._inception_graph)
      logging.info("KID score is %.3f", result_dict[self._KID_SCORE])
    return result_dict


class MultiscaleSSIMTask(EvalTask):
  """Task that computes MSSIMScore for generated images."""

  _MS_SSIM = "ms_ssim"

  def MetricsList(self):
    return frozenset([self._MS_SSIM])

  # 'RunInSession' it not needed for this task.

  def RunAfterSession(self, options, fake_images, real_images):
    del real_images
    result_dict = {}
    if ShouldRunMultiscaleSSIM(options):
      result_dict[self._MS_SSIM] = ComputeMultiscaleSSIMScore(fake_images)
      logging.info("MS-SSIM score computed: %.3f", result_dict[self._MS_SSIM])
    return result_dict


class ComputeAccuracyTask(EvalTask):
  """Task that computes the accuracy over test/train/fake data."""

  def MetricsList(self):
    return frozenset([
        "train_accuracy", "test_accuracy", "fake_accuracy", "train_d_loss",
        "test_d_loss"
    ])

  # RunAfterSession is not needed for this task.

  def RunInSession(self, options, sess, gan, real_images):
    if ShouldRunAccuracyLossTrainVsTest(options):
      return ComputeAccuracyLoss(options, sess, gan, real_images)
    else:
      return {}


def ComputeFractalDimension(fake_images,
                            num_fd_seeds=100,
                            n_bins=1000,
                            scale=0.1):
  """Compute Fractal Dimension of fake_images.

  Args:
    fake_images: an np array of datapoints, the dimensionality and scaling of
      images can be arbitrary
    num_fd_seeds: number of random centers from which fractal dimension
      computation is performed
     n_bins: number of bins to split the range of distance values into
     scale: the scale of the y interval in the log-log plot for which we apply a
       linear regression fit

  Returns:
    fractal dimension of the dataset.
  """
  assert len(fake_images.shape) >= 2
  assert fake_images.shape[0] >= num_fd_seeds

  num_images = fake_images.shape[0]
  # In order to apply scipy function we need to flatten the number of dimensions
  # to 2
  fake_images = np.reshape(fake_images, (num_images, -1))
  fake_images_subset = fake_images[np.random.randint(
      num_images, size=num_fd_seeds)]

  distances = scipy.spatial.distance.cdist(fake_images,
                                           fake_images_subset).flatten()
  min_distance = np.min(distances[np.nonzero(distances)])
  max_distance = np.max(distances)
  buckets = min_distance * (
      (max_distance / min_distance)**np.linspace(0, 1, n_bins))
  # Create a table where first column corresponds to distances r
  # and second column corresponds to number of points N(r) that lie
  # within distance r from the random seeds
  fd_result = np.zeros((n_bins - 1, 2))
  fd_result[:, 0] = buckets[1:]
  fd_result[:, 1] = np.sum(np.less.outer(distances, buckets[1:]), axis=0)

  # We compute the slope of the log-log plot at the middle y value
  # which is stored in y_val; the linear regression fit is computed on
  # the part of the plot that corresponds to an interval around y_val
  # whose size is 2*scale*(total width of the y axis)
  max_y = np.log(num_images * num_fd_seeds)
  min_y = np.log(num_fd_seeds)
  x = np.log(fd_result[:, 0])
  y = np.log(fd_result[:, 1])
  y_width = max_y - min_y
  y_val = min_y + 0.5 * y_width

  start = np.argmax(y > y_val - scale * y_width)
  end = np.argmax(y > y_val + scale * y_width)

  slope = np.linalg.lstsq(
      a=np.vstack([x[start:end], np.ones(end - start)]).transpose(),
      b=y[start:end].reshape(end - start, 1))[0][0][0]
  return slope


class FractalDimensionTask(EvalTask):
  """Fractal dimension metric."""

  _FRACTAL_DIMENSION = "fractal_dimension"

  def MetricsList(self):
    return frozenset([self._FRACTAL_DIMENSION])

  def RunAfterSession(self, options, fake_images, real_images):
    del real_images
    result_dict = {}
    if options.get("compute_fractal_dimension", False):
      result_dict[self._FRACTAL_DIMENSION] = ComputeFractalDimension(
          fake_images)
    return result_dict


class GILBOTask(EvalTask):
  """Compute GILBO metric and related consistency metrics."""

  def __init__(self, outdir, task_workdir, dataset_name):
    self.outdir = outdir
    self.task_workdir = task_workdir
    self.dataset = dataset_name

  def MetricsList(self):
    return frozenset([
        "gilbo",
        "gilbo_train_consistency",
        "gilbo_eval_consistency",
        "gilbo_self_consistency",
    ])

  def RunInSession(self, options, sess, gan, real_images):
    del real_images
    result_dict = {}
    if options.get("compute_gilbo", False):
      (gilbo, gilbo_train_consistency,
       gilbo_eval_consistency, gilbo_self_consistency) = gilbo_lib.TrainGILBO(
           gan, sess, self.outdir, self.task_workdir, self.dataset, options)
      result_dict["gilbo"] = gilbo
      result_dict["gilbo_train_consistency"] = gilbo_train_consistency
      result_dict["gilbo_eval_consistency"] = gilbo_eval_consistency
      result_dict["gilbo_self_consistency"] = gilbo_self_consistency

    return result_dict


def ComputeGeneratorConditionNumber(sess, gan):
  """Computes the generator condition number.

  Computes the Jacobian of the generator in session, then postprocesses to get
  the condition number.

  Args:
    sess: tf.Session object.
    gan: AbstractGAN object, that is already present in the current tf.Graph.

  Returns:
    A list of length gan.batch_size. Each element is the condition number
    computed at a single z sample within a minibatch.
  """
  shape = gan.fake_images.get_shape().as_list()
  flat_generator_output = tf.reshape(
      gan.fake_images, [gan.batch_size, np.prod(shape[1:])])
  tf_jacobian = conditioning_lib.compute_jacobian(
      xs=gan.z, fx=flat_generator_output)
  z_sample = gan.z_generator(gan.batch_size, gan.z_dim)
  np_jacobian = sess.run(tf_jacobian, feed_dict={gan.z: z_sample})
  result_dict = conditioning_lib.analyze_jacobian(np_jacobian)
  return result_dict["metric_tensor"]["log_condition_number"]


class GeneratorConditionNumberTask(EvalTask):
  """Computes the generator condition number.

  Computes the condition number for metric Tensor of the generator Jacobian.
  This condition number is computed locally for each z sample in a minibatch.
  Returns the mean log condition number and standard deviation across the
  minibatch.

  Follows the methods in https://arxiv.org/abs/1802.08768.
  """

  _CONDITION_NUMBER_COUNT = "log_condition_number_count"
  _CONDITION_NUMBER_MEAN = "log_condition_number_mean"
  _CONDITION_NUMBER_STD = "log_condition_number_std"

  def MetricsList(self):
    return frozenset([
        self._CONDITION_NUMBER_COUNT, self._CONDITION_NUMBER_MEAN,
        self._CONDITION_NUMBER_STD
    ])

  def RunInSession(self, options, sess, gan, real_images):
    del real_images
    result_dict = {}
    if options.get("compute_generator_condition_number", False):
      result = ComputeGeneratorConditionNumber(sess, gan)
      result_dict[self._CONDITION_NUMBER_COUNT] = len(result)
      result_dict[self._CONDITION_NUMBER_MEAN] = np.mean(result)
      result_dict[self._CONDITION_NUMBER_STD] = np.std(result)
    return result_dict


class NanFoundError(Exception):
  """Exception thrown, when the Nans are present in the output."""


def RunCheckpointEval(checkpoint_path, task_workdir, options, tasks_to_run):
  """Evaluate model at given checkpoint_path.

  Args:
    checkpoint_path: string, path to the single checkpoint to evaluate.
    task_workdir: directory, where results and logs can be written.
    options: Dict[Text, Text] with all parameters for the current trial.
    tasks_to_run: List of objects that inherit from EvalTask.

  Returns:
    Dict[Text, float] with all the computed results.

  Raises:
    NanFoundError: If gan output has generated any NaNs.
    ValueError: If options["gan_type"] is not supported.
  """

  # Make sure that the same latent variables are used for each evaluation.
  np.random.seed(42)

  checkpoint_dir = os.path.join(task_workdir, "checkpoint")
  result_dir = os.path.join(task_workdir, "result")
  gan_log_dir = os.path.join(task_workdir, "logs")

  gan_type = options["gan_type"]
  if gan_type not in SUPPORTED_GANS:
    raise ValueError("Gan type %s is not supported." % gan_type)

  dataset = options["dataset"]
  dataset_params = params.GetDatasetParameters(dataset)
  dataset_params.update(options)
  num_test_examples = dataset_params.get("eval_test_samples", 10000)

  if num_test_examples % INCEPTION_BATCH != 0:
    logging.info("Padding number of examples to fit inception batch.")
    num_test_examples -= num_test_examples % INCEPTION_BATCH

  real_images = GetRealImages(
      options["dataset"],
      split_name="test",
      num_examples=num_test_examples)
  logging.info("Real data processed.")

  result_dict = {}
  # Get Fake images from the generator.
  samples = []
  logging.info("Running eval for dataset %s, checkpoint: %s, num_examples: %d ",
               dataset, checkpoint_path, num_test_examples)
  with tf.Graph().as_default():
    with tf.Session() as sess:
      gan = gan_lib.create_gan(
          gan_type=gan_type,
          dataset=dataset,
          dataset_content=None,
          options=options,
          checkpoint_dir=checkpoint_dir,
          result_dir=result_dir,
          gan_log_dir=gan_log_dir)

      gan.build_model(is_training=False)

      tf.global_variables_initializer().run()
      saver = tf.train.Saver()
      saver.restore(sess, checkpoint_path)

      # Make sure we have >= examples as in the test set.
      num_batches = int(np.ceil(num_test_examples / gan.batch_size))
      for _ in range(num_batches):
        z_sample = gan.z_generator(gan.batch_size, gan.z_dim)
        x = sess.run(gan.fake_images, feed_dict={gan.z: z_sample})
        # If NaNs were generated, ignore this checkpoint and assign a very high
        # FID score which we handle specially later.
        if np.isnan(x).any():
          logging.error("Detected NaN in fake_images! Returning NaN.")
          raise NanFoundError("Detected NaN in fake images.")
        samples.append(x)

      print("Fake data generated, running tasks in session.")
      for task in tasks_to_run:
        result_dict.update(task.RunInSession(options, sess, gan, real_images))

  fake_images = np.concatenate(samples, axis=0)
  # Adjust the number of fake images to the number of images in the test set.
  fake_images = fake_images[:num_test_examples, :, :, :]

  assert fake_images.shape == real_images.shape

  # In case we use a 1-channel dataset (like mnist) - convert it to 3 channel.
  if fake_images.shape[3] == 1:
    fake_images = np.tile(fake_images, [1, 1, 1, 3])
    # change the real_images' shape too - so that it keep matching
    # fake_images' shape.
    real_images = np.tile(real_images, [1, 1, 1, 3])

  fake_images *= 255.0

  logging.info("Fake data processed. Starting tasks for checkpoint: %s.",
               checkpoint_path)

  for task in tasks_to_run:
    result_dict.update(task.RunAfterSession(options, fake_images, real_images))

  return result_dict


def RunTaskEval(options, task_workdir, inception_graph, out_file="scores.csv"):
  """Evaluates all checkpoints for the given task.

  Fetches all the checkpoints that exists in the workdir and evaluates each one.
  Final scores are written into the out_file and the best fid_score is also
  stored in the "value" file in the task_workdir.

  Args:
    options: Dict[Text, Text] with all parameters for the current trial.
    task_workdir: Directory where checkpoints are present. All scores will be
      written there too.
    inception_graph: GraphDef that contains inception model (used for FID
      computation).
    out_file: name of the file to store final outputs.
  """
  outdir = options.get("eval_outdir", task_workdir)

  tasks_to_run = [
      InceptionScoreTask(inception_graph),
      FIDScoreTask(inception_graph),
      MultiscaleSSIMTask(),
      ComputeAccuracyTask(),
      FractalDimensionTask(),
      KIDScoreTask(inception_graph),
      GeneratorConditionNumberTask(),
      GILBOTask(outdir, task_workdir, options["dataset"]),
  ]

  # If the output file doesn't exist, create it.
  csv_header = [
      "checkpoint_path",
      "model",
      "dataset",
      "tf_seed",
      "sample_id",
  ]
  task_headers = []
  for task in tasks_to_run:
    task_headers.extend(sorted(task.MetricsList()))
  csv_header.extend(task_headers)

  train_params = GetAllTrainingParams()
  csv_header.extend(train_params)

  scores_path = os.path.join(outdir, out_file)

  if not tf.gfile.Exists(scores_path):
    with tf.gfile.Open(scores_path, "w") as f:
      writer = csv.DictWriter(f, fieldnames=csv_header)
      writer.writeheader()

  # Get the list of records that were already computed, to not re-do them.
  finished_checkpoints = set()
  try:
    with tf.gfile.Open(scores_path, "r") as f:
      reader = csv.DictReader(f)
      for row in reader:
        if sorted(csv_header) != sorted(list(row.keys())):
          raise ValueError("wrong csv keys.")
        finished_checkpoints.add(row["checkpoint_path"])
  except ValueError:
    logging.error("CSV headers no longer match. Recomputing all results.")
    finished_checkpoints = {}
    with tf.gfile.Open(scores_path, "w") as f:
      writer = csv.DictWriter(f, fieldnames=csv_header)
      writer.writeheader()

  # Compute all records not done yet.
  with tf.gfile.Open(scores_path, "a") as f:
    writer = csv.writer(f)
    checkpoint_dir = os.path.join(task_workdir, "checkpoint")
    # Fetch checkpoint to eval.
    checkpoint_state = tf.train.get_checkpoint_state(checkpoint_dir)

    all_checkpoint_paths = checkpoint_state.all_model_checkpoint_paths
    for checkpoint_path in all_checkpoint_paths:
      if checkpoint_path in finished_checkpoints:
        logging.info("Skipping already computed path: %s", checkpoint_path)
        continue

      # Write the FID score and all training params.
      default_value = -1.0
      try:
        result_dict = RunCheckpointEval(checkpoint_path, task_workdir, options,
                                        tasks_to_run)
      except NanFoundError as nan_found_error:
        result_dict = {}

        logging.error(nan_found_error)
        default_value = NAN_DETECTED

      logging.info(result_dict)
      tf_seed = str(options.get("tf_seed", -1))
      sample_id = str(options.get("sample_id", -1))
      output_row = [
          checkpoint_path, options["gan_type"], options["dataset"], tf_seed,
          sample_id
      ]

      for task_metric in task_headers:
        output_row.append("%.3f" % result_dict.get(task_metric, default_value))

      for param in train_params:
        if param in options:
          output_row.append(options[param])
        else:
          output_row.append(str(DEFAULT_VALUES[param]))
      writer.writerow(output_row)

      f.flush()


def SaveFinalEvaluationScore(scores_path, metric_name, output_path):
  """Get final evaluation score (lowest "metric_name") and save in the file.

  Reads the lowest "metric_name" value from the scores and
  writes it in the output_path.
  Args:
    scores_path: string, CSV file with the scores.
    metric_name: string, name of the metric/column in CSV.
    output_path: string, file to write the result to.
  """
  data = pd.read_csv(tf.gfile.Open(scores_path), sep=",")
  min_score = min(data[metric_name])
  with tf.gfile.Open(output_path, mode="w") as f:
    f.write(str(min_score))
