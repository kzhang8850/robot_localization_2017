#!/usr/bin/env python

"""
 Computational Introduction to Robotics Localization Project
 by Kevin Zhang and Shane Kelly
 Spring 2017

 This script is a completed version of the Particle Filter, meant to localize a robot Neato
 and help it determine where it is in relation to a room or a space.

 The code was scaffolded such that logistical and set up was given to us, and we wrote
 the bulk of the codebase, which was the particle filter itself. Our work can be summarized in 4 steps:

 1 - Initialize particle cloud
 2 - Update Particles based on how Neato last moved
 3 - Re-weight particles based on accuracy with Neato's laser measurements
 4 - Resample particles to reflect the probability distribution made by re-weighting

 Repeat until particles converge on Neato's location


 The current status of the codebase is fully functional and accurate to .2 meters or 20 degrees.
"""

import rospy

from std_msgs.msg import Header, String
from sensor_msgs.msg import LaserScan, PointCloud
from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped, PoseArray, Pose, Point, Quaternion
from nav_msgs.srv import GetMap
from copy import deepcopy

import tf
from tf import TransformListener
from tf import TransformBroadcaster
from tf.transformations import euler_from_quaternion, rotation_matrix, quaternion_from_matrix
from random import gauss

import math
import time

import numpy as np
from numpy.random import random_sample
from sklearn.neighbors import NearestNeighbors
from occupancy_field import OccupancyField

from helper_functions import (convert_pose_inverse_transform,
                              convert_translation_rotation_to_pose,
                              convert_pose_to_xy_and_theta,
                              angle_diff)


class Particle(object):
    """ Represents a hypothesis (particle) of the robot's pose consisting of x,y and theta (yaw)
        Attributes:
            x: the x-coordinate of the hypothesis relative to the map frame
            y: the y-coordinate of the hypothesis relative ot the map frame
            theta: the yaw of the hypothesis relative to the map frame
            w: the particle weight (the class does not ensure that particle weights are normalized
    """

    def __init__(self,x=0.0,y=0.0,theta=0.0,w=1.0):
        """ Construct a new Particle
            x: the x-coordinate of the hypothesis relative to the map frame
            y: the y-coordinate of the hypothesis relative ot the map frame
            theta: the yaw of the hypothesis relative to the map frame
            w: the particle weight (the class does not ensure that particle weights are normalized """
        self.w = w
        self.theta = theta
        self.x = x
        self.y = y


    def as_pose(self):
        """ A helper function to convert a particle to a geometry_msgs/Pose message """
        orientation_tuple = tf.transformations.quaternion_from_euler(0,0,self.theta)
        return Pose(position=Point(x=self.x,y=self.y,z=0), orientation=Quaternion(x=orientation_tuple[0], y=orientation_tuple[1], z=orientation_tuple[2], w=orientation_tuple[3]))


class ParticleFilter:
    """ The class that represents a Particle Filter ROS Node
        Attributes list:
            initialized: a Boolean flag to communicate to other class methods that initializaiton is complete
            base_frame: the name of the robot base coordinate frame (should be "base_link" for most robots)
            map_frame: the name of the map coordinate frame (should be "map" in most cases)
            odom_frame: the name of the odometry coordinate frame (should be "odom" in most cases)
            scan_topic: the name of the scan topic to listen to (should be "scan" in most cases)
            n_particles: the number of particles in the filter
            d_thresh: the amount of linear movement before triggering a filter update
            a_thresh: the amount of angular movement before triggering a filter update
            laser_max_distance: the maximum distance to an obstacle we should use in a likelihood calculation
            pose_listener: a subscriber that listens for new approximate pose estimates (i.e. generated through the rviz GUI)
            particle_pub: a publisher for the particle cloud
            laser_subscriber: listens for new scan data on topic self.scan_topic
            tf_listener: listener for coordinate transforms
            tf_broadcaster: broadcaster for coordinate transforms
            particle_cloud: a list of particles representing a probability distribution over robot poses
            current_odom_xy_theta: the pose of the robot in the odometry frame when the last filter update was performed.
                                   The pose is expressed as a list [x,y,theta] (where theta is the yaw)
            map: the map we will be localizing ourselves in.  The map should be of type nav_msgs/OccupancyGrid
    """
    def __init__(self):
        self.initialized = False        # make sure we don't perform updates before everything is setup
        rospy.init_node('pf')           # tell roscore that we are creating a new node named "pf"

        self.base_frame = "base_link"   # the frame of the robot base
        self.map_frame = "map"          # the name of the map coordinate frame
        self.odom_frame = "odom"        # the name of the odometry coordinate frame
        self.scan_topic = "scan"        # the topic where we will get laser scans from

        self.n_particles = 500          # the number of particles to use

        self.d_thresh = 0.1             # the amount of linear movement before performing an update
        self.a_thresh = math.pi/12       # the amount of angular movement before performing an update

        self.laser_max_distance = 2.0   # maximum penalty to assess in the likelihood field model

        # Setup pubs and subs

        # pose_listener responds to selection of a new approximate robot location (for instance using rviz)
        rospy.Subscriber("initialpose", PoseWithCovarianceStamped, self.update_initial_pose)

        # publish the current particle cloud.  This enables viewing particles in rviz.
        self.particle_pub = rospy.Publisher("particlecloud", PoseArray, queue_size=10)
        # laser_subscriber listens for data from the lidar
        rospy.Subscriber(self.scan_topic, LaserScan, self.scan_received)

        # enable listening for and broadcasting coordinate transforms
        self.tf_listener = TransformListener()
        self.tf_broadcaster = TransformBroadcaster()

        self.particle_cloud = []

        # change use_projected_stable_scan to True to use point clouds instead of laser scans
        self.use_projected_stable_scan = False
        self.last_projected_stable_scan = None
        if self.use_projected_stable_scan:
            # subscriber to the odom point cloud
            rospy.Subscriber("projected_stable_scan", PointCloud, self.projected_scan_received)

        self.current_odom_xy_theta = []

        # request the map from the map server
    	rospy.wait_for_service('static_map')
    	try:
            map_server = rospy.ServiceProxy('static_map', GetMap)
            map = map_server().map
            print map.info.resolution
    	except:
    		print "Service call failed!"

        # initializes the occupancyfield which contains the map
        self.occupancy_field = OccupancyField(map)
        print "initialized"
        self.initialized = True


    def update_robot_pose(self):
        """ Update the estimate of the robot's pose given the updated particles.
            There are two logical methods for this:
                (1): compute the mean pose
                (2): compute the most likely pose (i.e. the mode of the distribution)
        """
        # first make sure that the particle weights are normalized
        self.normalize_particles()

        # for the pose, calculate the particle's mean location
    	mean_particle = Particle(0, 0, 0, 0)
        mean_particle_theta_x = 0
        mean_particle_theta_y = 0
        for particle in self.particle_cloud:
            mean_particle.x += particle.x * particle.w
            mean_particle.y += particle.y * particle.w

            # angle is calculated using trig to account for angle runover
            distance_vector = np.sqrt(np.square(particle.y)+np.square(particle.x))
            mean_particle_theta_x += distance_vector * np.cos(particle.theta) * particle.w
            mean_particle_theta_y += distance_vector * np.sin(particle.theta) * particle.w

        mean_particle.theta = np.arctan2(float(mean_particle_theta_y),float(mean_particle_theta_x))

        self.robot_pose = mean_particle.as_pose()


    def projected_scan_received(self, msg):
        self.last_projected_stable_scan = msg


    def update_particles_with_odom(self, msg):
        """ Update the particles using the newly given odometry pose.
            The function computes the value delta which is a tuple (x,y,theta)
            that indicates the change in position and angle between the odometry
            when the particles were last updated and the current odometry.

            msg: this is not really needed to implement this, but is here just in case.
        """
        new_odom_xy_theta = convert_pose_to_xy_and_theta(self.odom_pose.pose)
        # compute the change in x,y,theta since our last update
        if self.current_odom_xy_theta:
            old_odom_xy_theta = self.current_odom_xy_theta
            delta = (new_odom_xy_theta[0] - self.current_odom_xy_theta[0],
                     new_odom_xy_theta[1] - self.current_odom_xy_theta[1],
                     new_odom_xy_theta[2] - self.current_odom_xy_theta[2])

            self.current_odom_xy_theta = new_odom_xy_theta
        else:
            self.current_odom_xy_theta = new_odom_xy_theta
            return

        odom_noise = .3 # level of noise put into particles after update from odom to introduce variability

        # updates the particles based on r1, d, and r2. For more information on this, consult the website
    	for particle in self.particle_cloud:
            # calculates r1, d, and r2
            r1 = np.arctan2(float(delta[1]),float(delta[0])) - old_odom_xy_theta[2]
            d = np.sqrt(np.square(delta[0])+np.square(delta[1]))
            r2 = delta[2] - r1

            # updates the particles with the above variables, while also adding in some noise
            particle.theta = particle.theta + r1*(random_sample()*odom_noise+(1-odom_noise/2.0))
            particle.x = particle.x + d*np.cos(particle.theta)*(random_sample()*odom_noise+(1-odom_noise/2.0))
            particle.y = particle.y + d*np.sin(particle.theta)*(random_sample()*odom_noise+(1-odom_noise/2.0))
            particle.theta = particle.theta + r2*(random_sample()*odom_noise+(1-odom_noise/2.0))


    def resample_particles(self):
        """ Resample the particles according to the new particle weights.
            The weights stored with each particle should define the probability that a particular
            particle is selected in the resampling step.  You may want to make use of the given helper
            function draw_random_sample.
        """
        # make sure the distribution is normalized
        self.normalize_particles()

        # creates choices and probabilities lists, which are the particles and their respective weights
        choices = []
        probabilities = []
        num_samples = len(self.particle_cloud)
        for particle in self.particle_cloud:
            choices.append(particle)
            probabilities.append(particle.w)

        # re-makes the particle cloud according to a random sample based on the probability distribution of the weights
        self.particle_cloud = self.draw_random_sample(choices, probabilities, num_samples)

    def update_particles_with_laser(self, msg):
        """ Updates the particle weights in response to the scan contained in the msg """

        # for each particle, find the total error based on 36 laser measurements taken from the Neato's actual position
        for particle in self.particle_cloud:
            error = []
            for theta in range(0,360,10):
                rad = np.radians(theta)
                err = self.occupancy_field.get_closest_obstacle_distance(particle.x + msg.ranges[theta] * np.cos(particle.theta + rad), particle.y + msg.ranges[theta] * np.sin(particle.theta + rad))
                if (math.isnan(err)):   # if the get_closest_obstacle_distance method finds that a point is out of bounds, then the particle can't never be it
                    particle.w = 0
                    break
                error.append(err**5)     # each error is appended up to a power to make more likely particles have higher probability
            if (sum(error) == 0):     # if the particle is basically a perfect match, then we make the particle almost always enter the next iteration through resampling
                particle.w = 1.0
            else:
                particle.w = 1.0/sum(error)   # the errors are inverted such that large errors become small and small errors become large


    @staticmethod
    def draw_random_sample(choices, probabilities, n):
        """ Return a random sample of n elements from the set choices with the specified probabilities
            choices: the values to sample from represented as a list
            probabilities: the probability of selecting each element in choices represented as a list
            n: the number of samples
        """
        # sets up an index list for the chosen particles, and makes bins for the probabilities
        values = np.array(range(len(choices)))
        probs = np.array(probabilities)
        bins = np.add.accumulate(probs)
        inds = values[np.digitize(random_sample(n), bins)]  # chooses the new particles based on the probabilities of the old ones
        samples = []
        for i in inds:
            samples.append(deepcopy(choices[int(i)]))   # makes the new particle cloud based on the chosen particles
        return samples

    def update_initial_pose(self, msg):
        """ Callback function to handle re-initializing the particle filter based on a pose estimate.
            These pose estimates could be generated by another ROS Node or could come from the rviz GUI """
        xy_theta = convert_pose_to_xy_and_theta(msg.pose.pose)
        self.initialize_particle_cloud(xy_theta)
        self.fix_map_to_odom_transform(msg)

    def initialize_particle_cloud(self, xy_theta=None):
        """ Initialize the particle cloud.
            Arguments
            xy_theta: a triple consisting of the mean x, y, and theta (yaw) to initialize the
                      particle cloud around.  If this input is ommitted, the odometry will be used """

        # levels of noise to introduce variability
        lin_noise = 1
        ang_noise = math.pi/2.0

        #  if doesn't exist, use odom
        if xy_theta == None:
            xy_theta = convert_pose_to_xy_and_theta(self.odom_pose.pose)

        # make a new particle cloud, and then create a bunch of particles at the initial location with some added noise
        self.particle_cloud = []
    	for x in range(self.n_particles):
    		x = xy_theta[0] + (random_sample()*lin_noise-(lin_noise/2.0))
    		y = xy_theta[1] + (random_sample()*lin_noise-(lin_noise/2.0))
    		theta = xy_theta[2] + (random_sample()*ang_noise-(ang_noise/2.0))
    		self.particle_cloud.append(Particle(x, y, theta))

        # normalize particles because all weights were originall set to 1 on default
        self.normalize_particles()
        self.update_robot_pose()

    def normalize_particles(self):
        """ Make sure the particle weights define a valid distribution (i.e. sum to 1.0) """
        # takes the sum, and then divides all weights by the sum
    	weights_sum = sum(particle.w for particle in self.particle_cloud)
        for particle in self.particle_cloud:
            particle.w /= weights_sum

    def publish_particles(self, msg):
        """Publishes the particles out for visualization and other purposes"""
        particles_conv = []
        for p in self.particle_cloud:
            particles_conv.append(p.as_pose())
        # actually send the message so that we can view it in rviz
        self.particle_pub.publish(PoseArray(header=Header(stamp=rospy.Time.now(),
                                            frame_id=self.map_frame),
                                  poses=particles_conv))

    def scan_received(self, msg):
        """ This is the default logic for what to do when processing scan data.
            Feel free to modify this, however, I hope it will provide a good
            guide.  The input msg is an object of type sensor_msgs/LaserScan """
        if not(self.initialized):
            # wait for initialization to complete
            return

        if not(self.tf_listener.canTransform(self.base_frame,msg.header.frame_id,msg.header.stamp)):
            # need to know how to transform the laser to the base frame
            # this will be given by either Gazebo or neato_node
            return

        if not(self.tf_listener.canTransform(self.base_frame,self.odom_frame,msg.header.stamp)):
            # need to know how to transform between base and odometric frames
            # this will eventually be published by either Gazebo or neato_node
            return

        # calculate pose of laser relative ot the robot base
        p = PoseStamped(header=Header(stamp=rospy.Time(0),
                                      frame_id=msg.header.frame_id))
        self.laser_pose = self.tf_listener.transformPose(self.base_frame,p)

        # find out where the robot thinks it is based on its odometry
        p = PoseStamped(header=Header(stamp=msg.header.stamp,
                                      frame_id=self.base_frame),
                        pose=Pose())
        self.odom_pose = self.tf_listener.transformPose(self.odom_frame, p)
        # store the the odometry pose in a more convenient format (x,y,theta)
        new_odom_xy_theta = convert_pose_to_xy_and_theta(self.odom_pose.pose)
        if not(self.particle_cloud):
            # now that we have all of the necessary transforms we can update the particle cloud
            self.initialize_particle_cloud()
            # cache the last odometric pose so we can only update our particle filter if we move more than self.d_thresh or self.a_thresh
            self.current_odom_xy_theta = new_odom_xy_theta
            # update our map to odom transform now that the particles are initialized
            self.fix_map_to_odom_transform(msg)
        elif (math.fabs(new_odom_xy_theta[0] - self.current_odom_xy_theta[0]) > self.d_thresh or
              math.fabs(new_odom_xy_theta[1] - self.current_odom_xy_theta[1]) > self.d_thresh or
              math.fabs(new_odom_xy_theta[2] - self.current_odom_xy_theta[2]) > self.a_thresh):
            # we have moved far enough to do an update!
            self.update_particles_with_odom(msg)    # update based on odometry
            if self.last_projected_stable_scan:
                last_projected_scan_timeshift = deepcopy(self.last_projected_stable_scan)
                last_projected_scan_timeshift.header.stamp = msg.header.stamp
                self.scan_in_base_link = self.tf_listener.transformPointCloud("base_link", last_projected_scan_timeshift)

            self.update_particles_with_laser(msg)   # update based on laser scan
            self.update_robot_pose()                # update robot's pose
            self.resample_particles()               # resample particles to focus on areas of high density
            self.fix_map_to_odom_transform(msg)     # update map to odom transform now that we have new particles
        # publish particles (so things like rviz can see them)
        self.publish_particles(msg)

    def fix_map_to_odom_transform(self, msg):
        """ This method constantly updates the offset of the map and
            odometry coordinate systems based on the latest results from
            the localizer"""
        (translation, rotation) = convert_pose_inverse_transform(self.robot_pose)
        p = PoseStamped(pose=convert_translation_rotation_to_pose(translation,rotation),
                        header=Header(stamp=msg.header.stamp,frame_id=self.base_frame))
        self.tf_listener.waitForTransform(self.base_frame, self.odom_frame, msg.header.stamp, rospy.Duration(1.0))
        self.odom_to_map = self.tf_listener.transformPose(self.odom_frame, p)
        (self.translation, self.rotation) = convert_pose_inverse_transform(self.odom_to_map.pose)

    def broadcast_last_transform(self):
        """ Make sure that we are always broadcasting the last map
            to odom transformation.  This is necessary so things like
            move_base can work properly. """
        if not(hasattr(self,'translation') and hasattr(self,'rotation')):
            return
        self.tf_broadcaster.sendTransform(self.translation,
                                          self.rotation,
                                          rospy.get_rostime(),
                                          self.odom_frame,
                                          self.map_frame)

if __name__ == '__main__':
    n = ParticleFilter()
    r = rospy.Rate(5)

    while not(rospy.is_shutdown()):
        # in the main loop all we do is continuously broadcast the latest map to odom transform
        n.broadcast_last_transform()
        r.sleep()
