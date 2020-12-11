from dynamic_stack_decider.abstract_decision_element import AbstractDecisionElement


class BallInDefensiveArea(AbstractDecisionElement):
    def __init__(self, blackboard, dsd, parameters=None):
        super(BallInDefensiveArea, self).__init__(blackboard, dsd, parameters)
        self.defensive_area = self.blackboard.config['defensive_area']

    def perform(self, reevaluate=False):
        """
        Determines whether the ball is in the defensive area of the field as defined in the config.
        :param reevaluate:
        :return:
        """
        ball_position = self.blackboard.world_model.get_ball_position_xy()
        # calculate the x value of the boundary of the defensive area
        defensive_x = (self.defensive_area * self.blackboard.world_model.field_length) - (self.blackboard.world_model.field_length / 2)
        if ball_position[0] <= defensive_x:
            return 'YES'
        return 'NO'

    def get_reevaluate(self):
        return True
