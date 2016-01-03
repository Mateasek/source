
from time import time
from math import tan, pi, ceil
from multiprocessing import Process, cpu_count, Queue

import numpy as np
from numpy import zeros
from matplotlib.pyplot import imshow, imsave, show, clf, draw, pause

from raysect.core import World, AffineMatrix3D, Point3D, Vector3D, Observer
from raysect.optical.colour import resample_ciexyz, spectrum_to_ciexyz, ciexyz_to_srgb
from raysect.core.math import random


class Camera(Observer):

    def __init__(self, pixels=(512, 512), sensitivity=1.0, spectral_samples=20, spectral_rays=1, pixel_samples=100,
                 process_count=cpu_count(), parent=None, transform=AffineMatrix3D(), name=None):
        """

        :param pixels:
        :param sensitivity:
        :param spectral_samples: number of wavelength bins between min/max wavelengths.
        :param spectral_rays: number of rays to use when sub-sampling the overall spectral range. This must be ~15 or
        greater to see dispersion effects in materials.
        :param pixel_samples:
        :param process_count:
        :param parent:
        :param transform:
        :param name:
        :return:
        """

        super().__init__(parent, transform, name)

        # ray configuration
        self.spectral_rays = spectral_rays
        self.spectral_samples = spectral_samples
        self.min_wavelength = 375.0
        self.max_wavelength = 740.0
        self.ray_extinction_prob = 0.1
        self.ray_min_depth = 3
        self.ray_max_depth = 100

        # progress information
        self.display_progress = True
        self.display_update_time = 10.0

        # concurrency configuration
        self.process_count = process_count

        # camera configuration
        self._pixels = np.empty(pixels, dtype=object)
        self.sensitivity = sensitivity
        self.pixel_samples = pixel_samples
        self.accumulate = False
        self.accumulated_samples = 0

        # output from last call to Observe()
        self.xyz_frame = zeros((pixels[1], pixels[0], 3))
        self.frame = zeros((pixels[1], pixels[0], 3))

    @property
    def pixels(self):
        return self._pixels

    @pixels.setter
    def pixels(self, pixels):
        raise RuntimeError("Camera attribute pixels is read only.")

    def _rebuild_pixels(self):
        """
        Virtual method - to be implemented by derived classes.

        Runs at the start of observe() loop to set up any data needed for calculating pixel vectors
        and super-sampling that shouldn't be calculated at every loop iteration. The result of this
        function should be written to self._pixel_vectors_variables.
        """
        raise NotImplementedError("Virtual method _setup_pixels() has not been implemented for this Camera.")

    def observe(self):

        # must be connected to a world node to be able to perform a ray trace
        if not isinstance(self.root, World):
            raise TypeError("Observer is not connected to a scene graph containing a World object.")

        # create intermediate and final frame-buffers
        if not self.accumulate:
            self.xyz_frame = zeros((self._pixels[1], self._pixels[0], 3))
            self.frame = zeros((self._pixels[1], self._pixels[0], 3))
            self.accumulated_samples = 0

        # generate spectral data
        wvl_channels = self._calc_wvl_channel_config()

        # rebuild pixels in case camera properties have changed
        self._rebuild_pixels()

        # trace
        if self.process_count == 1:
            self._observe_single(self.xyz_frame, wvl_channels)
        else:
            self._observe_parallel(self.xyz_frame, wvl_channels)

        # update sample accumulation statistics
        self.accumulated_samples += self.pixel_samples * self.spectral_rays

    def _observe_single(self, xyz_frame, wvl_channels):

        # initialise user interface
        display_timer = self._start_display()
        statistics_data = self._start_statistics()

        # generate weightings for accumulation
        total_samples = self.accumulated_samples + self.pixel_samples * self.spectral_rays
        previous_weight = self.accumulated_samples / total_samples
        added_weight = self.spectral_rays * self.pixel_samples / total_samples

        # scale previous state to account for additional samples
        xyz_frame[:, :, :] = previous_weight * xyz_frame[:, :, :]

        # render
        for i_channel, wvl_channel_config in enumerate(wvl_channels):

            min_wavelength, max_wavelength, spectral_samples = wvl_channel_config

            # generate resampled XYZ curves for channel spectral range
            resampled_xyz = resample_ciexyz(min_wavelength, max_wavelength, spectral_samples)

            nx, ny = self._pixels.shape
            for iy in range(ny):
                for ix in range(nx):

                    # load selected pixel and trace rays
                    pixel = self.pixels[ix, iy]
                    spectrum, ray_count = pixel.sample_pixel(min_wavelength, max_wavelength, spectral_samples, self)

                    # convert spectrum to CIE XYZ
                    xyz = spectrum_to_ciexyz(spectrum, resampled_xyz)

                    xyz_frame[iy, ix, 0] += added_weight * xyz[0]
                    xyz_frame[iy, ix, 1] += added_weight * xyz[1]
                    xyz_frame[iy, ix, 2] += added_weight * xyz[2]

                    # convert to sRGB colour-space
                    self.frame[iy, ix, :] = ciexyz_to_srgb(*xyz_frame[iy, ix, :])

                    # update users
                    pixel = ix + self._pixels[0] * iy
                    statistics_data = self._update_statistics(statistics_data, i_channel, pixel, ray_count)
                    display_timer = self._update_display(display_timer)

        # final update for users
        self._final_statistics(statistics_data)
        self._final_display()

    def _observe_parallel(self, xyz_frame, wvl_channels):

        world = self.root

        # initialise user interface
        display_timer = self._start_display()
        statistics_data = self._start_statistics()

        total_pixels = self._pixels[0] * self._pixels[1]

        # generate weightings for accumulation
        total_samples = self.accumulated_samples + self.pixel_samples * self.rays
        previous_weight = self.accumulated_samples / total_samples
        added_weight = self.rays * self.pixel_samples / total_samples

        # scale previous state to account for additional samples
        xyz_frame[:, :, :] = previous_weight * xyz_frame[:, :, :]

        # render
        for channel, wvl_channel_config in enumerate(wvl_channels):

            min_wavelength, max_wavelength, spectral_samples = wvl_channel_config

            # generate resampled XYZ curves for channel spectral range
            resampled_xyz = resample_ciexyz(min_wavelength, max_wavelength, spectral_samples)

            # establish ipc queues using a manager process
            task_queue = Queue()
            result_queue = Queue()

            # start process to generate image samples
            producer = Process(target=self._producer, args=(task_queue, ))
            producer.start()

            # start worker processes
            workers = []
            for pid in range(self.process_count):
                p = Process(target=self._worker,
                            args=(world, min_wavelength, max_wavelength, spectral_samples,
                                  resampled_xyz, task_queue, result_queue))
                p.start()
                workers.append(p)

            # collect results
            for pixel in range(total_pixels):

                # obtain result
                location, xyz, sample_ray_count = result_queue.get()
                x, y = location

                xyz_frame[y, x, 0] += added_weight * xyz[0]
                xyz_frame[y, x, 1] += added_weight * xyz[1]
                xyz_frame[y, x, 2] += added_weight * xyz[2]

                # convert to sRGB colour-space
                self.frame[y, x, :] = ciexyz_to_srgb(*xyz_frame[y, x, :])

                # update users
                display_timer = self._update_display(display_timer)
                statistics_data = self._update_statistics(statistics_data, channel, pixel, sample_ray_count)

            # shutdown workers
            for _ in workers:
                task_queue.put(None)

        # final update for users
        self._final_statistics(statistics_data)
        self._final_display()

    def _producer(self, task_queue):
        # task is simply the pixel location
        nx, ny = self._pixels
        for y in range(ny):
            for x in range(nx):
                task_queue.put((x, y))

    def _worker(self, world, min_wavelength, max_wavelength, spectral_samples, resampled_xyz, task_queue, result_queue):

        # re-seed the random number generator to prevent all workers inheriting the same sequence
        random.seed()

        while True:

            # request next pixel
            pixel = task_queue.get()

            # have we been commanded to shutdown?
            if pixel is None:
                break

            x, y = pixel
            spectrum, ray_count = self._sample_pixel(x, y, min_wavelength, max_wavelength, spectral_samples,
                                                     world)

            # convert spectrum to CIE XYZ
            xyz = spectrum_to_ciexyz(spectrum, resampled_xyz)

            # encode result and send
            result = (pixel, xyz, ray_count)
            result_queue.put(result)

    def _calc_wvl_channel_config(self):
        """
        Break the wavelength range up based on the number of required spectral rays. When simulated dispersion effects
        or reflections for example, the overall wavelength range may be broken up into >20 sub regions for individual
        ray sampling.

        :return: list[tuples (min_wavelength, max_wavelength, spectral_samples),...]
        """

        # TODO - spectral_samples needs to be over whole wavelength range, not each sub wavelength range.
        config = []
        delta_wavelength = (self.max_wavelength - self.min_wavelength) / self.spectral_rays
        for index in range(self.spectral_rays):
            config.append((self.min_wavelength + delta_wavelength * index,
                           self.min_wavelength + delta_wavelength * (index + 1),
                           self.spectral_samples))
        return config

    def _start_display(self):
        """
        Display live render.
        """

        display_timer = 0
        if self.display_progress:
            self.display()
            display_timer = time()
        return display_timer

    def _update_display(self, display_timer):
        """
        Update live render.
        """

        # update live render display
        if self.display_progress and (time() - display_timer) > self.display_update_time:

            print("Refreshing display...")
            self.display()
            display_timer = time()

        return display_timer

    def _final_display(self):
        """
        Display final frame.
        """

        if self.display_progress:
            self.display()

    def _start_statistics(self):
        """
        Initialise statistics.
        """

        total_pixels = self._pixels[0] * self._pixels[1]
        total_work = total_pixels * self.rays
        ray_count = 0
        start_time = time()
        progress_timer = time()
        return total_work, total_pixels, ray_count, start_time, progress_timer

    def _update_statistics(self, statistics_data, index, pixel, sample_ray_count):
        """
        Display progress statistics.
        """

        total_work, total_pixels, ray_count, start_time, progress_timer = statistics_data
        ray_count += sample_ray_count

        if (time() - progress_timer) > 1.0:

            current_work = total_pixels * index + pixel
            completion = 100 * current_work / total_work
            print("{:0.2f}% complete (channel {}/{}, line {}/{}, pixel {}/{}, {:0.1f}k rays)".format(
                completion, index + 1, self.rays,
                ceil((pixel + 1) / self._pixels[0]), self._pixels[1],
                pixel + 1, total_pixels, ray_count / 1000))
            ray_count = 0
            progress_timer = time()

        return total_work, total_pixels, ray_count, start_time, progress_timer

    def _final_statistics(self, statistics_data):
        """
        Final statistics output.
        """
        _, _, _, start_time, _ = statistics_data
        elapsed_time = time() - start_time
        print("Render complete - time elapsed {:0.3f}s".format(elapsed_time))

    # def _get_pixel_rays(self, x, y, min_wavelength, max_wavelength, spectral_samples, pixel_configuration):
    #     """
    #     Virtual method - to be implemented by derived classes.
    #
    #     Called for each pixel in the _worker() observe loop. For a given pixel, this function must return a list of
    #     vectors to ray trace.
    #     """
    #     raise NotImplementedError("Virtual method _get_pixel_vectors() has not been implemented for this Camera.")

    def display(self):
        clf()
        imshow(self.frame, aspect="equal", origin="upper")
        draw()
        show()
        # workaround for interactivity for QT backend
        pause(0.1)

    def save(self, filename):
        imsave(filename, self.frame)


class PinholeCamera(Camera):

    def __init__(self, pixels=(512, 512), fov=45, sensitivity=1.0, spectral_samples=20, rays=1, pixel_samples=100,
                 sub_sample=False, process_count=cpu_count(), parent=None, transform=AffineMatrix3D(), name=None):

        super().__init__(pixels=pixels, sensitivity=sensitivity, spectral_samples=spectral_samples, rays=rays,
                         pixel_samples=pixel_samples, process_count=process_count, parent=parent,
                         transform=transform, name=name)

        self._fov = fov
        self.sub_sample = sub_sample

    @property
    def fov(self):
        return self._fov

    @fov.setter
    def fov(self, fov):
        if fov <= 0:
            raise ValueError("Field of view angle can not be less than or equal to 0 degrees.")
        self._fov = fov

    def _setup_pixel_config(self):

        max_pixels = max(self._pixels)

        if max_pixels > 1:
            # generate ray directions by simulating an image plane 1m from pinhole "aperture"
            # max width of image plane at 1 meter for given field of view
            image_max_width = 2 * tan(pi / 180 * 0.5 * self._fov)

            # pixel step and start point in image plane
            image_delta = image_max_width / (max_pixels - 1)

            # start point of scan in image plane
            image_start_x = 0.5 * self._pixels[0] * image_delta
            image_start_y = 0.5 * self._pixels[1] * image_delta

        else:
            # single ray on axis
            image_delta = 0
            image_start_x = 0
            image_start_y = 0

        origin = Point3D(0, 0, 0).transform(self.to_root())

        return origin, image_delta, image_start_x, image_start_y


