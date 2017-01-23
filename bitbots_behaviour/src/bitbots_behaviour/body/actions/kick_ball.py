"""
KickBall
^^^^^^^^

.. moduleauthor:: Martin Poppinga <1popping@informatik.uni-hamburg.de>
"""
import time

from stackmachine.abstract_action_module import AbstractActionModule

from stackmachine.model import Connector
import rospy


class KickBall(AbstractActionModule):
    """
    Kickt nach dem Ball, bekommt im init_data die Information ob links oder rechts gekickt werden soll
    Kicks the ball, gets in init_data the information about the side.
    """

    def __init__(self, args):
        super(KickBall, self).__init__()
        self.side = args
        self.begin = time.time()
        self.rk = rospy.get_param("/animations/kicks/rk")
        self.lk = rospy.get_param("/animations/kicks/lk")
        self.rkp = rospy.get_param("/animations/kicks/rkp")
        self.lkp = rospy.get_param("/animations/kicks/lkp")
        self.side_right = rospy.get_param("/animations/kicks/lko")
        self.side_left = rospy.get_param("/animations/kicks/rko")
        self.anim_begin = False

    def perform(self, connector: Connector, reevaluate=False):
        self.do_not_reevaluate()
        connector.walking.stop_walking()

        if time.time() - self.begin > 3.5:  # wait one moment
            self.do_not_reevaluate()  # dont interrrupt the kick

            connector.blackboard.set_one_time_kicked(True)
            # todo make a better behaviour that looks if the ball realy moved

            if not connector.animation.is_animation_busy() and self.anim_begin:
                # if the animation was performed, jump one level higher
                return self.interrupt()

            self.anim_begin = True

            if self.side == "R":
                connector.animation.play_animation(self.rk)
            elif self.side == "L":
                connector.animation.play_animation(self.lk)
            elif self.side == "RP":
                connector.animation.play_animation(self.rkp)
            elif self.side == "LP":
                connector.animation.play_animation(self.lkp)
            elif self.side == "SLK":
                connector.animation.play_animation(self.side_left)
            elif self.side == "SRK":
                connector.animation.play_animation(self.side_right)
            else:
                raise NotImplementedError("This kick does not exist")
