"""
WorldModelCapsule
^^^^^^^^^^^^^^^^^^

Provides information about the world model.
"""
import math
import ros_numpy
import numpy as np
from scipy.ndimage import gaussian_filter
import matplotlib.pyplot as plt
from scipy.interpolate import griddata
from PIL import Image, ImageDraw

import rospy
import tf2_ros as tf2
from std_msgs.msg import Header
from std_srvs.srv import Trigger
from tf2_geometry_msgs import PointStamped
from geometry_msgs.msg import Point, PoseWithCovarianceStamped, TwistWithCovarianceStamped, TwistStamped, PoseStamped, \
    Quaternion, Pose, TransformStamped
from nav_msgs.msg import OccupancyGrid, MapMetaData
from tf.transformations import euler_from_quaternion, quaternion_from_euler
from humanoid_league_msgs.msg import PoseWithCertaintyArray, PoseWithCertainty
import sensor_msgs.point_cloud2 as pc2


class GoalRelative:
    header = Header()
    left_post = Point()
    right_post = Point()

    def to_pose_with_certainty_array(self):
        p = PoseWithCertaintyArray()
        p.header = self.header
        l = PoseWithCertainty()
        l.pose.pose.position = self.left_post
        r = PoseWithCertainty()
        r.pose.pose.position = self.right_post
        p.poses = [l, r]
        return p


class WorldModelCapsule:
    def __init__(self, blackboard):
        self._blackboard = blackboard
        self.body_config = rospy.get_param("behavior/body")
        # This pose is not supposed to be used as robot pose. Just as precision measurement for the TF position.
        self.pose = PoseWithCovarianceStamped()
        self.tf_buffer = tf2.Buffer(cache_time=rospy.Duration(30))
        self.tf_listener = tf2.TransformListener(self.tf_buffer)

        self.odom_frame = rospy.get_param('~odom_frame', 'odom')
        self.map_frame = rospy.get_param('~map_frame', 'map')
        self.ball_frame = rospy.get_param('~ball_frame', 'ball')
        self.base_footprint_frame = rospy.get_param('~base_footprint_frame', 'base_footprint')

        self.ball = PointStamped()  # The ball in the base footprint frame
        self.ball_odom = PointStamped()  # The ball in the odom frame (when localization is not usable)
        self.ball_odom.header.stamp = rospy.Time(0)
        self.ball_odom.header.frame_id = self.odom_frame
        self.ball_map = PointStamped()  # The ball in the map frame (when localization is usable)
        self.ball_map.header.stamp = rospy.Time(0)
        self.ball_map.header.frame_id = self.map_frame
        self.ball_teammate = PointStamped()
        self.ball_teammate.header.stamp = rospy.Time(0)
        self.ball_teammate.header.frame_id = self.map_frame
        self.ball_lost_time = rospy.Duration(rospy.get_param('behavior/body/ball_lost_time', 8.0))
        self.ball_twist_map = None
        self.ball_filtered = None
        self.ball_twist_lost_time = rospy.Duration(rospy.get_param('behavior/body/ball_twist_lost_time', 2))
        self.ball_twist_precision_threshold = rospy.get_param('behavior/body/ball_twist_precision_threshold', None)
        self.reset_ball_filter = rospy.ServiceProxy('ball_filter_reset', Trigger)

        self.goal = GoalRelative()  # The goal in the base footprint frame
        self.goal_odom = GoalRelative()
        self.goal_odom.header.stamp = rospy.Time.now()
        self.goal_odom.header.frame_id = self.odom_frame

        self.my_data = dict()
        self.counter = 0
        self.ball_seen_time = rospy.Time(0)
        self.ball_seen_time_teammate = rospy.Time(0)
        self.goal_seen_time = rospy.Time(0)
        self.ball_seen = False
        self.ball_seen_teammate = False
        self.field_length = rospy.get_param('field_length', None)
        self.field_width = rospy.get_param('field_width', None)
        self.goal_width = rospy.get_param('goal_width', None)
        self.map_margin = rospy.get_param('behavior/body/map_margin', 1.0)
        self.obstacle_costmap_smoothing_sigma = rospy.get_param("behavior/body/obstacle_costmap_smoothing_sigma", 1.0)
        self.obstacle_cost = rospy.get_param("behavior/body/obstacle_cost", 1.0)

        self.use_localization = rospy.get_param('behavior/body/use_localization', None)

        self.pose_precision_threshold = rospy.get_param('behavior/body/pose_precision_threshold', None)

        # Publisher for visualization in RViZ
        self.ball_publisher = rospy.Publisher('debug/viz_ball', PointStamped, queue_size=1)
        self.goal_publisher = rospy.Publisher('debug/viz_goal', PoseWithCertaintyArray, queue_size=1)
        self.ball_twist_publisher = rospy.Publisher('debug/ball_twist', TwistStamped, queue_size=1)
        self.used_ball_pub = rospy.Publisher('debug/used_ball', PointStamped, queue_size=1)
        self.which_ball_pub = rospy.Publisher('debug/which_ball_is_used', Header, queue_size=1)
        self.costmap_publisher = rospy.Publisher('debug/costmap', OccupancyGrid, queue_size=1)

        self.base_costmap = None  # generated once in constructor field features
        self.costmap = None  # updated on the fly based on the base_costmap
        self.gradient_map = None  # global heading map (static) only dependent on field structure

        # Calculates the base costmap and gradient map based on it
        self.calc_base_costmap()
        self.calc_gradients()

    ############
    ### Ball ###
    ############

    def ball_seen_self(self):
        """Returns true if we have seen the ball recently (less than ball_lost_time ago)"""
        return rospy.Time.now() - self.ball_seen_time < self.ball_lost_time

    def ball_last_seen(self):
        """
        Returns the time at which the ball was last seen if it is in the threshold or
        the more recent ball from either the teammate or itself if teamcom is available
        """
        if self.ball_seen_self() or not hasattr(self._blackboard, "team_data"):
            return self.ball_seen_time
        else:
            if self.use_localization and self.localization_precision_in_threshold():
                # better value of teammate and us if we are localized
                return max(self.ball_seen_time, self._blackboard.team_data.get_teammate_ball_seen_time())
            else:
                # can't use teammate ball if we dont know where we are
                return self.ball_seen_time

    def ball_has_been_seen(self):
        """Returns true if we or a teammate have seen the ball recently (less than ball_lost_time ago)"""
        return rospy.Time.now() - self.ball_last_seen() < self.ball_lost_time

    def get_ball_position_xy(self):
        """Return the ball saved in the map or odom frame"""
        ball = self.get_best_ball_point_stamped()
        return ball.point.x, ball.point.y

    def get_ball_stamped_relative(self):
        """ Returns the ball in the base_footprint frame i.e. relative to the robot projected on the ground"""
        return self.ball

    def get_best_ball_point_stamped(self):
        """
        Returns the best ball, either its own ball has been in the ball_lost_lost time
        or from teammate if the robot itself has lost it and teamcom is available
        """
        if self.use_localization and self.localization_precision_in_threshold():
            if self.ball_seen_self() or not hasattr(self._blackboard, "team_data"):
                self.used_ball_pub.publish(self.ball_map)
                h = Header()
                h.stamp = self.ball_map.header.stamp
                h.frame_id = "own_ball_map"
                self.which_ball_pub.publish(h)
                return self.ball_map
            else:
                teammate_ball = self._blackboard.team_data.get_teammate_ball()
                if teammate_ball is not None and self.tf_buffer.can_transform(self.base_footprint_frame,
                                                                              teammate_ball.header.frame_id,
                                                                              teammate_ball.header.stamp,
                                                                              timeout=rospy.Duration(0.2)):
                    self.used_ball_pub.publish(teammate_ball)
                    h = Header()
                    h.stamp = teammate_ball.header.stamp
                    h.frame_id = "teammate_ball"
                    self.which_ball_pub.publish(h)
                    return teammate_ball
                else:
                    rospy.logerr("our ball is bad but the teammates ball is worse or cant be transformed")
                    h = Header()
                    h.stamp = self.ball_map.header.stamp
                    h.frame_id = "own_ball_map"
                    self.which_ball_pub.publish(h)
                    self.used_ball_pub.publish(self.ball_map)
                    return self.ball_map
        else:
            h = Header()
            h.stamp = self.ball_odom.header.stamp
            h.frame_id = "own_ball_odom"
            self.which_ball_pub.publish(h)
            self.used_ball_pub.publish(self.ball_odom)
            return self.ball_odom

    def get_ball_position_uv(self):
        ball = self.get_best_ball_point_stamped()
        try:
            ball_bfp = self.tf_buffer.transform(ball, self.base_footprint_frame, timeout=rospy.Duration(0.2)).point
        except (tf2.ExtrapolationException) as e:
            rospy.logwarn(e)
            rospy.logerr('Severe transformation problem concerning the ball!')
            return None
        return ball_bfp.x, ball_bfp.y

    def get_ball_distance(self, filtered=False):
        if filtered:
            u = self.ball_filtered.pose.pose.position.x
            v = self.ball_filtered.pose.pose.position.y
        else:
            ball_pos = self.get_ball_position_uv()
            if ball_pos is None:
                return np.inf  # worst case (very far away)
            else:
                u, v = ball_pos
        return math.sqrt(u ** 2 + v ** 2)

    def get_ball_angle(self):
        ball_pos = self.get_ball_position_uv()
        if ball_pos is None:
            return -math.pi  # worst case (behind robot)
        else:
            u, v = ball_pos
        return math.atan2(v, u)

    def get_ball_speed(self):
        raise NotImplementedError

    def ball_filtered_callback(self, msg: PoseWithCovarianceStamped):
        self.ball_filtered = msg

        # When the precision is not sufficient, the ball ages.
        x_sdev = msg.pose.covariance[0]  # position 0,0 in a 6x6-matrix
        y_sdev = msg.pose.covariance[7]  # position 1,1 in a 6x6-matrix
        if x_sdev > self.body_config['ball_position_precision_threshold']['x_sdev'] or \
                y_sdev > self.body_config['ball_position_precision_threshold']['y_sdev']:
            self.forget_ball(own=True, team=False, reset_ball_filter=False)
            return

        ball_buffer = PointStamped(msg.header, msg.pose.pose.position)
        try:
            self.ball = self.tf_buffer.transform(ball_buffer, self.base_footprint_frame, timeout=rospy.Duration(0.3))
            self.ball_odom = self.tf_buffer.transform(ball_buffer, self.odom_frame, timeout=rospy.Duration(0.3))
            self.ball_map = self.tf_buffer.transform(ball_buffer, self.map_frame, timeout=rospy.Duration(0.3))
            # Set timestamps to zero to get the newest transform when this is transformed later
            self.ball_odom.header.stamp = rospy.Time(0)
            self.ball_map.header.stamp = rospy.Time(0)
            self.ball_seen_time = rospy.Time.now()
            self.ball_publisher.publish(self.ball)
            self.ball_seen = True

        except (tf2.ConnectivityException, tf2.LookupException, tf2.ExtrapolationException) as e:
            rospy.logwarn(e)

    def recent_ball_twist_available(self):
        if self.ball_twist_map is None:
            return False
        return rospy.Time.now() - self.ball_twist_map.header.stamp < self.ball_twist_lost_time

    def ball_twist_callback(self, msg: TwistWithCovarianceStamped):
        x_sdev = msg.twist.covariance[0]  # position 0,0 in a 6x6-matrix
        y_sdev = msg.twist.covariance[7]  # position 1,1 in a 6x6-matrix
        if x_sdev > self.ball_twist_precision_threshold['x_sdev'] or \
                y_sdev > self.ball_twist_precision_threshold['y_sdev']:
            return
        if msg.header.frame_id != self.map_frame:
            try:
                # point (0,0,0)
                point_a = PointStamped()
                point_a.header = msg.header
                # linear velocity vector
                point_b = PointStamped()
                point_b.header = msg.header
                point_b.point.x = msg.twist.twist.linear.x
                point_b.point.y = msg.twist.twist.linear.y
                point_b.point.z = msg.twist.twist.linear.z
                # transform start and endpoint of velocity vector
                point_a = self.tf_buffer.transform(point_a, self.map_frame, timeout=rospy.Duration(0.3))
                point_b = self.tf_buffer.transform(point_b, self.map_frame, timeout=rospy.Duration(0.3))
                # build new twist using transform vector
                self.ball_twist_map = TwistStamped(header=msg.header)
                self.ball_twist_map.header.frame_id = self.map_frame
                self.ball_twist_map.twist.linear.x = point_b.point.x - point_a.point.x
                self.ball_twist_map.twist.linear.y = point_b.point.y - point_a.point.y
                self.ball_twist_map.twist.linear.z = point_b.point.z - point_a.point.z
            except (tf2.ConnectivityException, tf2.LookupException, tf2.ExtrapolationException) as e:
                rospy.logwarn(e)
        else:
            self.ball_twist_map = TwistStamped(header=msg.header, twist=msg.twist.twist)
        if self.ball_twist_map is not None:
            self.ball_twist_publisher.publish(self.ball_twist_map)

    def forget_ball(self, own=True, team=True, reset_ball_filter=True):
        """
        Forget that we and the best teammate saw a ball, optionally reset the ball filter
        :param own: Forget the ball recognized by the own robot, defaults to True
        :type own: bool, optional
        :param team: Forget the ball received from the team, defaults to True
        :type team: bool, optional
        :param reset_ball_filter: Reset the ball filter, defaults to True
        :type reset_ball_filter: bool, optional
        """
        if own:  # Forget own ball
            self.ball_seen_time = rospy.Time(0)
            self.ball = PointStamped()

        if team:  # Forget team ball
            self.ball_seen_time_teammate = rospy.Time(0)
            self.ball_teammate = PointStamped()

        if reset_ball_filter:  # Reset the ball filter
            result = self.reset_ball_filter()
            if result.success:
                rospy.loginfo(f"Received message from ball filter: '{result.message}'", logger_name='bitbots_blackboard')
            else:
                rospy.logwarn(f"Ball filter reset failed with: '{result.message}'", logger_name='bitbots_blackboard')

    ###########
    # ## Goal #
    ###########

    def goal_last_seen(self):
        # We are currently not seeing any goal, we know where they are based
        # on the localisation. Therefore, any_goal_last_seen returns the time
        # from the stamp of the last position update
        return self.goal_seen_time

    def get_map_based_opp_goal_center_uv(self):
        x, y = self.get_map_based_opp_goal_center_xy()
        return self.get_uv_from_xy(x, y)

    def get_map_based_opp_goal_center_xy(self):
        return self.field_length / 2, 0

    def get_map_based_own_goal_center_uv(self):
        x, y = self.get_map_based_own_goal_center_xy()
        return self.get_uv_from_xy(x, y)

    def get_map_based_own_goal_center_xy(self):
        return -self.field_length / 2, 0

    def get_map_based_opp_goal_angle_from_ball(self):
        ball_x, ball_y = self.get_ball_position_xy()
        goal_x, goal_y = self.get_map_based_opp_goal_center_xy()
        return math.atan2(goal_y - ball_y, goal_x - ball_x)

    def get_map_based_opp_goal_distance(self):
        x, y = self.get_map_based_opp_goal_center_xy()
        return self.get_distance_to_xy(x, y)

    def get_map_based_opp_goal_angle(self):
        x, y = self.get_map_based_opp_goal_center_uv()
        return math.atan2(y, x)

    def get_map_based_opp_goal_left_post_uv(self):
        x, y = self.get_map_based_opp_goal_center_xy()
        return self.get_uv_from_xy(x, y - self.goal_width / 2)

    def get_map_based_opp_goal_right_post_uv(self):
        x, y = self.get_map_based_opp_goal_center_xy()
        return self.get_uv_from_xy(x, y + self.goal_width / 2)

    def get_detection_based_goal_position_uv(self):
        """
        returns the position of the goal relative to the robot.
        if only a single post is detected, the position of the post is returned.
        else, it is the point between the posts
        :return:
        """
        left = PointStamped(self.goal_odom.header, self.goal_odom.left_post)
        right = PointStamped(self.goal_odom.header, self.goal_odom.right_post)
        left.header.stamp = rospy.Time(0)
        right.header.stamp = rospy.Time(0)
        try:
            left_bfp = self.tf_buffer.transform(left, self.base_footprint_frame, timeout=rospy.Duration(0.2)).point
            right_bfp = self.tf_buffer.transform(right, self.base_footprint_frame, timeout=rospy.Duration(0.2)).point
        except tf2.ExtrapolationException as e:
            rospy.logwarn(e)
            rospy.logerr('Severe transformation problem concerning the goal!')
            return None

        return (left_bfp.x + right_bfp.x / 2.0), \
               (left_bfp.y + right_bfp.y / 2.0)

    def goal_parts_callback(self, msg):
        # type: (GoalPartsRelative) -> None
        goal_parts = msg

    def goalposts_callback(self, goal_parts: PoseWithCertaintyArray):
        # todo: transform to base_footprint too!
        # adding a minor delay to timestamp to ease transformations.
        goal_parts.header.stamp = goal_parts.header.stamp + rospy.Duration.from_sec(0.01)

        # Tuple(First Post, Second Post, Distance)
        goal_combination = (-1, -1, -1)
        # Enumerate all goalpost combinations, this also combines each post with itself,
        # to get the special case that only one post was detected and the maximum distance is 0.
        for first_post_id, first_post in enumerate(goal_parts.poses):
            for second_post_id, second_post in enumerate(goal_parts.poses):
                # Get the minimal angular difference between the two posts
                first_post_pos = first_post.pose.pose.position
                second_post_pos = second_post.pose.pose.position
                angular_distance = abs((math.atan2(first_post_pos.x, first_post_pos.y) - math.atan2(
                    second_post_pos.x, second_post_pos.y) + math.pi) % (2 * math.pi) - math.pi)
                # Set a new pair of posts if the distance is bigger than the previous ones
                if angular_distance > goal_combination[2]:
                    goal_combination = (first_post_id, second_post_id, angular_distance)
        # Catch the case, that no posts are detected
        if goal_combination[2] == -1:
            return
        # Define right and left post
        first_post = goal_parts.poses[goal_combination[0]].pose.pose.position
        second_post = goal_parts.poses[goal_combination[1]].pose.pose.position
        if math.atan2(first_post.y, first_post.x) > \
                math.atan2(first_post.y, first_post.x):
            left_post = first_post
            right_post = second_post
        else:
            left_post = second_post
            right_post = first_post

        self.goal.header = goal_parts.header
        self.goal.left_post = left_post
        self.goal.right_post = right_post

        self.goal_odom.header = goal_parts.header
        if goal_parts.header.frame_id != self.odom_frame:
            goal_left_buffer = PointStamped(goal_parts.header, left_post)
            goal_right_buffer = PointStamped(goal_parts.header, right_post)
            try:
                self.goal_odom.left_post = self.tf_buffer.transform(goal_left_buffer, self.odom_frame,
                                                                    timeout=rospy.Duration(0.2)).point
                self.goal_odom.right_post = self.tf_buffer.transform(goal_right_buffer, self.odom_frame,
                                                                     timeout=rospy.Duration(0.2)).point
                self.goal_odom.header.frame_id = self.odom_frame
                self.goal_seen_time = rospy.Time.now()
            except (tf2.ConnectivityException, tf2.LookupException, tf2.ExtrapolationException) as e:
                rospy.logwarn(e)
        else:
            self.goal_odom.left_post = left_post
            self.goal_odom.right_post = right_post
            self.goal_seen_time = rospy.Time.now()
        self.goal_publisher.publish(self.goal_odom.to_pose_with_certainty_array())

    ###########
    # ## Pose #
    ###########

    def pose_callback(self, pos: PoseWithCovarianceStamped):
        self.pose = pos

    def get_current_position(self):
        """
        Returns the current position as determined by the localization
        :returns x,y,theta
        """
        transform = self.get_current_position_transform()
        if transform is None:
            return None
        orientation = transform.transform.rotation
        theta = euler_from_quaternion([orientation.x, orientation.y, orientation.z, orientation.w])[2]
        return transform.transform.translation.x, transform.transform.translation.y, theta

    def get_current_position_pose_stamped(self) -> PoseStamped:
        """
        Returns the current position as determined by the localization as a PoseStamped
        """
        transform = self.get_current_position_transform()
        if transform is None:
            return None
        ps = PoseStamped()
        ps.header = transform.header
        ps.pose.position.x = transform.transform.translation.x
        ps.pose.position.y = transform.transform.translation.y
        ps.pose.position.z = transform.transform.translation.z
        ps.pose.orientation = transform.transform.rotation
        return ps

    def get_current_position_transform(self) -> TransformStamped:
        """
        Returns the current position as determined by the localization as a TransformStamped
        """
        try:
            # get the most recent transform
            transform = self.tf_buffer.lookup_transform(self.map_frame, self.base_footprint_frame, rospy.Time(0))
        except (tf2.LookupException, tf2.ConnectivityException, tf2.ExtrapolationException) as e:
            rospy.logwarn(e)
            return None
        return transform

    def get_localization_precision(self):
        """
        Returns the current localization precision based on the covariance matrix.
        """
        x_sdev = self.pose.pose.covariance[0]  # position 0,0 in a 6x6-matrix
        y_sdev = self.pose.pose.covariance[7]  # position 1,1 in a 6x6-matrix
        theta_sdev = self.pose.pose.covariance[35]  # position 5,5 in a 6x6-matrix
        return (x_sdev, y_sdev, theta_sdev)

    def localization_precision_in_threshold(self) -> bool:
        """
        Returns whether the last localization precision values were in the threshold defined in the settings.
        """
        # Check whether we can transform into and from the map frame seconds.
        if not self.localization_pose_current():
            return False
        # get the standard deviation values of the covariance matrix
        precision = self.get_localization_precision()
        # return whether those values are in the threshold
        return precision[0] < self.pose_precision_threshold['x_sdev'] and \
               precision[1] < self.pose_precision_threshold['y_sdev'] and \
               precision[2] < self.pose_precision_threshold['theta_sdev']

    def localization_pose_current(self) -> bool:
        """
        Returns whether we can transform into and from the map frame.
        """
        # if we can do this, we should be able to transform the ball
        # (unless the localization dies in the next 0.2 seconds)
        try:
            t = rospy.Time.now()-rospy.Duration(0.3)
        except TypeError as e:
            rospy.logerr(e)
            t = rospy.Time(0)
        return self.tf_buffer.can_transform(self.base_footprint_frame, self.map_frame, t)   

    #############
    # ## Common #
    #############

    def get_uv_from_xy(self, x, y):
        """ Returns the relativ positions of the robot to this absolute position"""
        current_position = self.get_current_position()
        x2 = x - current_position[0]
        y2 = y - current_position[1]
        theta = -1 * current_position[2]
        u = math.cos(theta) * x2 + math.sin(theta) * y2
        v = math.cos(theta) * y2 - math.sin(theta) * x2
        return u, v

    def get_xy_from_uv(self, u, v):
        """ Returns the absolute position from the given relative position to the robot"""
        pos_x, pos_y, theta = self.get_current_position()
        angle = math.atan2(v, u) + theta
        hypotenuse = math.sqrt(u ** 2 + v ** 2)
        return pos_x + math.sin(angle) * hypotenuse, pos_y + math.cos(angle) * hypotenuse

    def get_distance_to_xy(self, x, y):
        """ Returns distance from robot to given position """
        u, v = self.get_uv_from_xy(x, y)
        dist = math.sqrt(u ** 2 + v ** 2)
        return dist

    ############
    # Obstacle #
    ############

    def robot_obstacle_callback(self, msg):
        """
        Callback with new obstacles
        """
        # Init a new obstacle costmap
        obstacle_map = np.zeros_like(self.costmap)
        # Iterate over all obstacles
        for p in pc2.read_points(msg, field_names=("x", "y", "z"), skip_nans=True):
            # Convert position to array index
            idx_x, idx_y = self.field_2_costmap_coord(p[0], p[1])
            # Draw obstacle with smoothing independent weight on obstacle costmap
            obstacle_map[idx_x, idx_y] = \
                self.obstacle_cost * self.obstacle_costmap_smoothing_sigma
        # Smooth obstacle map
        obstacle_map = gaussian_filter(obstacle_map, self.obstacle_costmap_smoothing_sigma)
        # Get pass offsets
        self.pass_map = self.get_pass_regions()
        # Merge costmaps
        self.costmap = self.base_costmap.copy() + obstacle_map - self.pass_map
        # Publish debug costmap
        self.costmap_debug_draw()

    def costmap_debug_draw(self):
        """
        Publishes the costmap for rviz
        """
        # Normalize costmap to match the rviz color scheme in a good way
        normalized_costmap = (255 - ((self.costmap - np.min(self.costmap)) / (np.max(self.costmap) - np.min(self.costmap))) * 255 / 2.1).astype(np.int8).T
        # Build the OccupancyGrid message
        msg = ros_numpy.msgify(
            OccupancyGrid,
            normalized_costmap,
            info=MapMetaData(
                resolution=0.1,
                origin=Pose(
                    position=Point(
                        x=-self.field_length/2 - self.map_margin,
                        y=-self.field_width/2 - self.map_margin,
                    )
                )))
        # Change the frame to allow namespaces
        msg.header.frame_id = self.map_frame
        # Publish
        self.costmap_publisher.publish(msg)

    def get_pass_regions(self):
        """
        Draws a costmap for the pass regions
        """
        pass_dist = 1.0
        pass_weight = 20.0
        pass_smooth = 4.0
        # Init a new costmap
        costmap = np.zeros_like(self.costmap)
        # Iterate over possible team mate poses
        for pose in self._blackboard.team_data.get_active_teammate_poses(count_goalies=False):
            # Get positions
            goal_position = np.array([self.field_length / 2, 0, 0])  # position of the opponent goal
            teammate_position = ros_numpy.numpify(pose.position)
            # Get vector
            vector_teammate_to_goal = goal_position - ros_numpy.numpify(pose.position)
            # Position between robot and goal but 1m away from the robot
            pass_pos = vector_teammate_to_goal / np.linalg.norm(vector_teammate_to_goal) * pass_dist + teammate_position
            # Convert position to array index
            idx_x, idx_y = self.field_2_costmap_coord(pass_pos[0], pass_pos[1])
            # Draw pass position with smoothing independent weight on costmap
            costmap[idx_x, idx_y] = pass_weight * pass_smooth
        # Smooth obstacle map
        return gaussian_filter(costmap, pass_smooth)

    def field_2_costmap_coord(self, x, y):
        """
        Converts a field position to the coresponding indices for the costmap.

        :param x: X Position relative to the center point. (Positive is towards the enemy goal)
        :param y: Y Position relative to the center point. (Positive is towards the left when we face the enemy goal)
        :return: The x index of the coresponding costmap slot, The y index of the coresponding costmap slot
        """
        idx_x = int(min(((self.field_length + self.map_margin * 2) * 10) - 1,
                        max(0, (x + self.field_length / 2 + self.map_margin) * 10)))
        idx_y = int(min(((self.field_width + self.map_margin * 2) * 10) - 1,
                        max(0, (y + self.field_width / 2 + self.map_margin) * 10)))
        return idx_x, idx_y

    def calc_gradients(self):
        """
        Recalculates the gradient map based on the current costmap.
        """
        gradient = np.gradient(self.base_costmap)
        norms = np.linalg.norm(gradient, axis=0)

        # normalize gradient length
        gradient = [np.where(norms == 0, 0, i / norms) for i in gradient]
        self.gradient_map = gradient

    def cost_at_relative_xy(self, x, y):
        """
        Returns cost at relative position to the base footprint.
        """
        if self.costmap is None:
            return 0.0

        point = PointStamped()
        point.header.stamp = rospy.Time(0)
        point.header.frame_id = self.base_footprint_frame
        point.point.x = x
        point.point.y = y

        try:
            # Transform point of interest to the map
            point = self.tf_buffer.transform(point, self.map_frame, timeout=rospy.Duration(0.3))
        except (tf2.ConnectivityException, tf2.LookupException, tf2.ExtrapolationException) as e:
            rospy.logwarn(e)
            return 0.0

        return self.get_cost_at_field_position(point.point.x, point.point.y)

    def calc_base_costmap(self):
        """
        Builds the base costmap based on the bahavior parameters.
        This costmap includes a gradient towards the enemy goal and high costs outside the playable area
        """
        # Get parameters
        goalpost_safety_distance = rospy.get_param(
            "behavior/body/goalpost_safety_distance")  # offset in y direction from the goalpost
        keep_out_border = rospy.get_param("behavior/body/keep_out_border")  # dangerous border area
        in_field_value_our_side = rospy.get_param("behavior/body/in_field_value_our_side")  # start value on our side
        corner_value = rospy.get_param("behavior/body/corner_value")  # cost in a corner
        goalpost_value = rospy.get_param("behavior/body/goalpost_value")  # cost at a goalpost
        goal_value = rospy.get_param("behavior/body/goal_value")  # cost in the goal

        # Create Grid
        grid_x, grid_y = np.mgrid[
                         0:self.field_length + self.map_margin * 2:(self.field_length + self.map_margin * 2) * 10j,
                         0:self.field_width + self.map_margin * 2:(self.field_width + self.map_margin * 2) * 10j]

        fix_points = []

        # Add base points
        fix_points.extend([
            # Corner points of the map (including margin)
            [[-self.map_margin, -self.map_margin],
             corner_value + in_field_value_our_side],
            [[self.field_length + self.map_margin, -self.map_margin],
             corner_value + in_field_value_our_side],
            [[-self.map_margin, self.field_width + self.map_margin],
             corner_value + in_field_value_our_side],
            [[self.field_length + self.map_margin, self.field_width + self.map_margin],
             corner_value + in_field_value_our_side],
            # Corner points of the field
            [[0, 0],
             corner_value + in_field_value_our_side],
            [[self.field_length, 0],
             corner_value],
            [[0, self.field_width],
             corner_value + in_field_value_our_side],
            [[self.field_length, self.field_width],
             corner_value],
            # Points in the field that pull the gradient down, so we don't play always in the middle
            [[keep_out_border, keep_out_border],
             in_field_value_our_side],
            [[keep_out_border, self.field_width - keep_out_border],
             in_field_value_our_side],
        ])

        # Add goal area (including the dangerous parts on the side of the goal)
        fix_points.extend([
            [[self.field_length, self.field_width / 2 - self.goal_width / 2],
             goalpost_value],
            [[self.field_length, self.field_width / 2 + self.goal_width / 2],
             goalpost_value],
            [[self.field_length, self.field_width / 2 - self.goal_width / 2 + goalpost_safety_distance],
             goal_value],
            [[self.field_length, self.field_width / 2 + self.goal_width / 2 - goalpost_safety_distance],
             goal_value],
            [[self.field_length + self.map_margin,
              self.field_width / 2 - self.goal_width / 2 - goalpost_safety_distance],
             -0.2],
            [[self.field_length + self.map_margin,
              self.field_width / 2 + self.goal_width / 2 + goalpost_safety_distance],
             -0.2],
        ])

        # Apply map margin to fixpoints
        fix_points = [[[p[0][0] + self.map_margin, p[0][1] + self.map_margin], p[1]] for p in fix_points]

        # Interpolate the keypoints from above to form the costmap
        interpolated = griddata([p[0] for p in fix_points], [p[1] for p in fix_points], (grid_x, grid_y),
                                method='linear')

        # Smooth the costmap to get more continus gradients
        self.base_costmap = gaussian_filter(interpolated, rospy.get_param("behavior/body/base_costmap_smoothing_sigma"))
        self.costmap = self.base_costmap.copy()

        # plt.imshow(self.costmap, origin='lower')
        # plt.show()

    def get_gradient_at_field_position(self, x, y):
        """
        Gets the gradient tuple at a given field position
        :param x: Field coordiante in the x direction
        :param y: Field coordiante in the y direction
        """
        idx_x, idx_y = self.field_2_costmap_coord(x, y)
        return -self.gradient_map[0][idx_x, idx_y], -self.gradient_map[1][idx_x, idx_y]

    def get_cost_at_field_position(self, x, y):
        """
        Gets the costmap value at a given field position
        :param x: Field coordinate in the x direction
        :param y: Field coordinate in the y direction
        """
        idx_x, idx_y = self.field_2_costmap_coord(x, y)
        return self.costmap[idx_x, idx_y]

    def get_gradient_direction_at_field_position(self, x, y):
        """
        Returns the gradient direction at the given position
        :param x: Field coordiante in the x direction
        :param y: Field coordiante in the y direction
        """
        # for debugging only
        if False and self.costmap.sum() > 0:
            # Create Grid
            grid_x, grid_y = np.mgrid[0:self.field_length:self.field_length * 10j,
                             0:self.field_width:self.field_width * 10j]
            plt.imshow(self.costmap.T, origin='lower')
            plt.show()
            plt.quiver(grid_x, grid_y, -self.gradient_map[0], -self.gradient_map[1])
            plt.show()

        grad = self.get_gradient_at_field_position(x, y)
        return math.atan2(grad[1], grad[0])

    def get_cost_of_kick_relative(self, x, y, direction, kick_length, angular_range):
        if self.costmap is None:
            return 0.0

        pose = PoseStamped()
        pose.header.stamp = rospy.Time(0)
        pose.header.frame_id = self.base_footprint_frame
        pose.pose.position.x = x
        pose.pose.position.y = y

        pose.pose.orientation = Quaternion(*quaternion_from_euler(0, 0, direction))
        try:
            # Transform point of interest to the map
            pose = self.tf_buffer.transform(pose, self.map_frame, timeout=rospy.Duration(0.3))

        except (tf2.ConnectivityException, tf2.LookupException, tf2.ExtrapolationException) as e:
            rospy.logwarn(e)
            return 0.0
        d = euler_from_quaternion(
            [pose.pose.orientation.x, pose.pose.orientation.y, pose.pose.orientation.z, pose.pose.orientation.w])[2]
        return self.get_cost_of_kick(pose.pose.position.x, pose.pose.position.y, d, kick_length, angular_range)

    def get_cost_of_kick(self, x, y, direction, kick_length, angular_range):

        # create a mask in the size of the costmap consisting of 8-bit values initialized as 0
        mask = Image.new('L', (self.costmap.shape[1], self.costmap.shape[0]))

        # draw kick area on mask with ones
        maskd = ImageDraw.Draw(mask)
        # axes are switched in pillow

        b, a = self.field_2_costmap_coord(x, y)
        k = kick_length * 10
        m = a + k * math.sin(direction + 0.5 * angular_range)
        n = b + k * math.sin(0.5 * math.pi - (direction + 0.5 * angular_range))
        o = a + k * math.sin(direction - 0.5 * angular_range)
        p = b + k * math.sin(0.5 * math.pi - (direction - 0.5 * angular_range))
        maskd.polygon(((a, b), (m, n), (o, p)), fill=1)

        mask_array = np.array(mask)

        masked_costmap = self.costmap * mask_array

        # plt.imshow(self.costmap, origin='lower')
        # plt.show()
        # plt.imshow(masked_costmap, origin='lower')
        # plt.show()

        # The main influence should be the maximum cost in the area which is covered by the kick. This could be the field boundary, robots, ...
        # But we also want prio directions with lower min cost. This could be the goal area or the pass accept area of an teammate
        # This should contribute way less than the max and should have an impact if the max values are similar in all directions.
        return masked_costmap.max() * 0.75 + masked_costmap.min() * 0.25

    def get_current_cost_of_kick(self, direction, kick_length, angular_range):
        return self.get_cost_of_kick_relative(0, 0, direction, kick_length, angular_range)

    def get_best_kick_direction(self, min_angle, max_angle, num_kick_angles, kick_length, angular_range):
        # list of possible kick directions, sorted by absolute value to
        # prefer forward kicks to side kicks if their costs are equal
        kick_directions = sorted(np.linspace(min_angle,
                                             max_angle,
                                             num=num_kick_angles), key=abs)

        # get the kick direction with the least cost
        kick_direction = kick_directions[np.argmin([self.get_current_cost_of_kick(direction=direction,
                                                                                  kick_length=kick_length,
                                                                                  angular_range=angular_range)
                                                    for direction in kick_directions])]
        return kick_direction
