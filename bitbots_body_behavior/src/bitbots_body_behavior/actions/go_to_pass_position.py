import math

import rospy
from actionlib_msgs.msg import GoalStatus
from tf2_geometry_msgs import PoseStamped
from geometry_msgs.msg import Point

from dynamic_stack_decider.abstract_action_element import AbstractActionElement
from tf.transformations import quaternion_from_euler


class AbstractGoToPassPosition(AbstractActionElement):
    def __init__(self, blackboard, dsd, accept, parameters=None):
        super().__init__(blackboard, dsd, parameters)
        self.max_x = self.blackboard.config["supporter_max_x"]
        self.pass_pos_x = self.blackboard.config["pass_position_x"]
        self.pass_pos_y = self.blackboard.config["pass_position_y"]
        self.accept = accept

    def perform(self, reevaluate=False):
        # get ball pos
        ball_pos = self.blackboard.world_model.get_ball_position_xy()
        our_pose = self.blackboard.world_model.get_current_position()

        # decide on side
        if our_pose[1] < ball_pos[1]:
            side_sign = -1
        else:
            side_sign = 1

        # compute goal
        goal_x = ball_pos[0]
        if self.accept:
            goal_x += self.pass_pos_x

        # Limit the x position, so that we are not running into the enemy goal
        goal_x = min(self.max_x, goal_x)

        goal_y = ball_pos[1] + side_sign * self.pass_pos_y
        goal_yaw = 0

        pose_msg = PoseStamped()
        pose_msg.header.stamp = rospy.Time.now()
        pose_msg.header.frame_id = self.blackboard.map_frame
        pose_msg.pose.position.x = goal_x
        pose_msg.pose.position.y = goal_y
        quaternion = quaternion_from_euler(0, 0, goal_yaw)
        pose_msg.pose.orientation.x = quaternion[0]
        pose_msg.pose.orientation.y = quaternion[1]
        pose_msg.pose.orientation.z = quaternion[2]
        pose_msg.pose.orientation.w = quaternion[3]
        self.blackboard.pathfinding.publish(pose_msg)

        if self.blackboard.pathfinding.status in [GoalStatus.SUCCEEDED, GoalStatus.ABORTED]:
            self.pop()


class GoToPassPreparePosition(AbstractGoToPassPosition):
    """
    Go to a position 1m left or right from the ball (whichever is closer) as preparation for a pass
    """

    def __init__(self, blackboard, dsd, parameters=None):
        super().__init__(blackboard, dsd, False, parameters)


class GoToPassAcceptPosition(AbstractGoToPassPosition):
    """
    Go to a position forward of the ball to accept a pass from another robot.
    """

    def __init__(self, blackboard, dsd, parameters=None):
        super().__init__(blackboard, dsd, True, parameters)
