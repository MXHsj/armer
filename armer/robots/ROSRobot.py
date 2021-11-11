"""
ROSRobot module defines the ROSRobot type

.. codeauthor:: Gavin Suddreys
"""
import os
import timeit

from typing import List, Any
from threading import Lock, Event

import rospy
import actionlib
import tf
import roboticstoolbox as rtb
import spatialmath as sp
from spatialmath import SE3, SO3, UnitQuaternion
import numpy as np
import yaml

from scipy.interpolate import interp1d

from std_msgs.msg import Header
from sensor_msgs.msg import JointState
from geometry_msgs.msg import PoseStamped

from geometry_msgs.msg import TwistStamped, Twist
from std_srvs.srv import Empty, EmptyRequest, EmptyResponse

from std_msgs.msg import Float64MultiArray

from armer_msgs.msg import ManipulatorState, JointVelocity, Guards
from armer_msgs.msg import GuardedVelocityAction, GuardedVelocityGoal, GuardedVelocityResult
from armer_msgs.msg import MoveToJointPoseAction, MoveToJointPoseGoal, MoveToJointPoseResult
from armer_msgs.msg import MoveToNamedPoseAction, MoveToNamedPoseGoal, MoveToNamedPoseResult
from armer_msgs.msg import MoveToPoseAction, MoveToPoseGoal, MoveToPoseResult
from armer_msgs.msg import ServoToPoseAction, ServoToPoseGoal, ServoToPoseResult

from armer_msgs.srv import SetCartesianImpedance, \
    SetCartesianImpedanceRequest, \
    SetCartesianImpedanceResponse

from armer_msgs.srv import AddNamedPose, \
    AddNamedPoseRequest, \
    AddNamedPoseResponse

from armer_msgs.srv import AddNamedPoseConfig, \
    AddNamedPoseConfigRequest, \
    AddNamedPoseConfigResponse

from armer_msgs.srv import GetNamedPoseConfigs, \
    GetNamedPoseConfigsRequest, \
    GetNamedPoseConfigsResponse

from armer_msgs.srv import GetNamedPoses, \
    GetNamedPosesRequest, \
    GetNamedPosesResponse

from armer_msgs.srv import RemoveNamedPose, \
    RemoveNamedPoseRequest, \
    RemoveNamedPoseResponse

from armer_msgs.srv import RemoveNamedPoseConfig, \
    RemoveNamedPoseConfigRequest, \
    RemoveNamedPoseConfigResponse

# pylint: disable=too-many-instance-attributes


class ROSRobot(rtb.ERobot):
    """
    The ROSRobot class wraps the rtb.ERobot implementing basic ROS functionality
    """

    def __init__(self,
                 robot: rtb.robot.Robot,
                 name: str = None,
                 joint_state_topic: str = None,
                 joint_velocity_topic: str = None,
                 origin=None,
                 config_path=None,
                 readonly=False,
                 gripper=None,
                 frequency=None,
                 * args,
                 **kwargs):  # pylint: disable=unused-argument

        super().__init__(robot)
        self.__dict__.update(robot.__dict__)

        self.gripper = gripper if gripper else self.grippers[0].links[0].name
        self.name = name if name else self.name
        
        sorted_links=[]
        #sort links by parents starting from gripper
        link=self.link_dict[self.gripper]   
        while link is not None:
            sorted_links.append(link)
            link=link.parent
        sorted_links.reverse()

        self.joint_indexes = []
        self.joint_names = list(map(lambda link: link._joint_name, filter(lambda link: link.isjoint, sorted_links)))

        if origin:
            self.base = SE3(origin[:3]) @ SE3.RPY(origin[3:])

        self.frequency = frequency if frequency else rospy.get_param(joint_state_topic + '/frequency', 500)
        
        self.q = self.qr if hasattr(self, 'qr') else self.q # pylint: disable=no-member
        self.joint_states = None # Joint state message

        # Guards used to prevent multiple motion requests conflicting
        self.moving: bool = False
        self.last_moving: bool = False

        self.preempted: bool = False

        self.lock: Lock = Lock()
        self.event: Event = Event()

        # Arm state property
        self.state: ManipulatorState = ManipulatorState()

        self.e_v_frame: str = None # Expected cartesian velocity

        self.e_v: np.array = np.zeros(shape=(6,))  # cartesian motion
        self.j_v: np.array = np.zeros(
            shape=(len(self.q),)
        )  # expected joint velocity

        self.e_p = self.fkine(
            self.q, 
            start=self.base_link, 
            end=self.gripper
        ) # measured end-effector position

        self.last_update: float = 0
        self.last_tick: float = 0

        self.joint_subscriber = rospy.Subscriber(
            joint_state_topic if joint_state_topic else '/joint_states',
            JointState,
            self._state_cb
        )

        self.readonly = readonly

        self.joint_velocity_topic = joint_velocity_topic \
                if joint_velocity_topic \
                else '/joint_group_velocity_controller/command'

        if not self.readonly:
            self.joint_publisher = rospy.Publisher(
                self.joint_velocity_topic,
                Float64MultiArray,
                queue_size=1
            )

            # Create Transform Listener
            self.tf_listener = tf.TransformListener()

            self.config_path = config_path if config_path else os.path.join(
                os.getenv('HOME', '/root'),
                '.ros/configs/armer.yaml'
            )

            self.custom_configs: List[str] = []
            self.__load_config()

            # Services
            rospy.Service('{}/home'.format(self.name.lower()),
                          Empty, self.home_cb)
            rospy.Service('{}/recover'.format(self.name.lower()),
                          Empty, self.recover_cb)
            rospy.Service('{}/stop'.format(self.name.lower()),
                          Empty, self.preempt)

            # Publishers
            self.state_publisher: rospy.Publisher = rospy.Publisher(
                '{}/state'.format(self.name.lower()), ManipulatorState, queue_size=1
            )

            # Subscribers
            self.cartesian_velocity_subscriber: rospy.Subscriber = rospy.Subscriber(
                '{}/cartesian/velocity'.format(self.name.lower()
                                               ), TwistStamped, self.velocity_cb
            )
            self.joint_velocity_subscriber: rospy.Subscriber = rospy.Subscriber(
                '{}/joint/velocity'.format(self.name.lower()
                                           ), JointVelocity, self.joint_velocity_cb
            )

            # Action Servers
            self.velocity_server: actionlib.SimpleActionServer = actionlib.SimpleActionServer(
                '{}/cartesian/guarded_velocity'.format(self.name.lower()),
                GuardedVelocityAction,
                execute_cb=self.guarded_velocity_cb,
                auto_start=False
            )
            self.velocity_server.register_preempt_callback(self.preempt)
            self.velocity_server.start()

            self.pose_server: actionlib.SimpleActionServer = actionlib.SimpleActionServer(
                '{}/cartesian/pose'.format(self.name.lower()),
                MoveToPoseAction,
                execute_cb=self.pose_cb,
                auto_start=False
            )
            self.pose_server.register_preempt_callback(self.preempt)
            self.pose_server.start()

            self.pose_servo_server: actionlib.SimpleActionServer = actionlib.SimpleActionServer(
                '{}/cartesian/servo_pose'.format(self.name.lower()),
                ServoToPoseAction,
                execute_cb=self.servo_cb,
                auto_start=False
            )
            self.pose_servo_server.register_preempt_callback(self.preempt)
            self.pose_servo_server.start()

            self.joint_pose_server: actionlib.SimpleActionServer = actionlib.SimpleActionServer(
                '{}/joint/pose'.format(self.name.lower()),
                MoveToJointPoseAction,
                execute_cb=self.joint_pose_cb,
                auto_start=False
            )
            self.joint_pose_server.register_preempt_callback(self.preempt)
            self.joint_pose_server.start()

            self.named_pose_server: actionlib.SimpleActionServer = actionlib.SimpleActionServer(
                '{}/joint/named'.format(self.name.lower()),
                MoveToNamedPoseAction,
                execute_cb=self.named_pose_cb,
                auto_start=False
            )
            self.named_pose_server.register_preempt_callback(self.preempt)
            self.named_pose_server.start()

            rospy.Service(
                '{}/set_cartesian_impedance'.format(self.name.lower()),
                SetCartesianImpedance,
                self.set_cartesian_impedance_cb
            )

            rospy.Service('{}/get_named_poses'.format(self.name.lower()), GetNamedPoses,
                          self.get_named_poses_cb)

            rospy.Service('{}/set_named_pose'.format(self.name.lower()), AddNamedPose,
                          self.add_named_pose_cb)
            rospy.Service('{}/remove_named_pose'.format(self.name.lower()), RemoveNamedPose,
                          self.remove_named_pose_cb)

            rospy.Service(
                '{}/add_named_pose_config'.format(self.name.lower()),
                AddNamedPoseConfig,
                self.add_named_pose_config_cb
            )
            rospy.Service(
                '{}/remove_named_pose_config'.format(self.name.lower()),
                RemoveNamedPoseConfig,
                self.remove_named_pose_config_cb
            )
            rospy.Service(
                '{}/get_named_pose_configs'.format(self.name.lower()),
                GetNamedPoseConfigs,
                self.get_named_pose_configs_cb
            )

    def close(self):
        """
        Closes the action servers associated with this robot
        """
        self.pose_server.need_to_terminate = True
        self.pose_servo_server.need_to_terminate = True
        self.joint_pose_server.need_to_terminate = True
        self.named_pose_server.need_to_terminate = True

    def _state_cb(self, msg):
        if not self.joint_indexes:
            for joint_name in self.joint_names:
                self.joint_indexes.append(msg.name.index(joint_name))
        
        self.q = np.array(msg.position)[self.joint_indexes] if len(msg.position) == self.n else [0] * self.n
        self.joint_states = msg
        
    def velocity_cb(self, msg: TwistStamped) -> None:
        """
        ROS Service callback:
        Moves the arm at the specified cartesian velocity
        w.r.t. a target frame

        :param msg: [description]
        :type msg: TwistStamped
        """
        if self.moving:
            self.preempt()

        with self.lock:
            self.__vel_move(msg)

    def guarded_velocity_cb(self, msg: GuardedVelocityGoal) -> None:
        if self.moving:
            self.preempt()
        
        with self.lock:
            start_time = timeit.default_timer()
            triggered = 0
            
            while not self.preempted:
                triggered = self.test_guards(msg.guards, start_time=start_time)

                if triggered != 0:
                    break

                self.__vel_move(msg.twist_stamped)
                rospy.sleep(0.01)

            self.velocity_server.set_succeeded(GuardedVelocityResult(triggered=True))

    def joint_velocity_cb(self, msg: JointVelocity) -> None:
        """
        ROS Service callback:
        Moves the joints of the arm at the specified velocities

        :param msg: [description]
        :type msg: JointVelocity
        """
        if self.moving:
            self.preempt()

        with self.lock:
            self.j_v = np.array(msg.joints)
            self.last_update = timeit.default_timer()

    def pose_cb(self, goal: MoveToPoseGoal) -> None:
        """
        ROS Action Server callback:
        Moves the end-effector to the
        cartesian pose indicated by goal

        :param goal: [description]
        :type goal: MoveToPoseGoal
        """
        if self.moving:
            self.preempt()

        with self.lock:
            goal_pose = goal.pose_stamped

            if goal_pose.header.frame_id == '':
                goal_pose.header.frame_id = self.base_link.name

            goal_pose = self.tf_listener.transformPose(
                self.base_link.name,
                goal_pose
            )
            print(goal_pose.pose)
            pose = goal_pose.pose

            target = SE3(pose.position.x, pose.position.y, pose.position.z) * UnitQuaternion([
                pose.orientation.w,
                pose.orientation.x,
                pose.orientation.y,
                pose.orientation.z
            ]).SE3()

            print(target)
            dq = self.ikine_LMS(target, q0=self.q, end=self.gripper)
            traj = rtb.tools.trajectory.jtraj(self.q, dq.q, self.frequency)

            if self.__traj_move(traj, goal.speed if goal.speed else 0.2):
                self.pose_server.set_succeeded(MoveToPoseResult(success=True))
            else:
                self.pose_server.set_aborted(MoveToPoseResult(success=False))

    def servo_cb(self, goal: ServoToPoseGoal) -> None:
        """
        ROS Action Server callback:
        Servos the end-effector to the cartesian pose indicated by goal

        :param goal: [description]
        :type goal: ServoToPoseGoal

        This callback makes use of the roboticstoolbox p_servo function
        to generate velocities at each timestep.
        """
        if self.moving:
            self.preempt()

        with self.lock:
            goal_pose = goal.pose_stamped

            if goal_pose.header.frame_id == '':
                goal_pose.header.frame_id = self.base_link.name

            goal_pose = self.tf_listener.transformPose(
                self.base_link.name,
                goal_pose
            )
            pose = goal_pose.pose

            target = SE3(pose.position.x, pose.position.y, pose.position.z) * UnitQuaternion([
                pose.orientation.w,
                pose.orientation.x,
                pose.orientation.y,
                pose.orientation.z
            ]).SE3()

            arrived = False

            self.moving = True

            while not arrived and not self.preempted:
                velocities, arrived = rtb.p_servo(
                    self.fkine(self.q, start=self.base_link, end=self.gripper),
                    target,
                    min(3, goal.gain) if goal.gain else 2,
                    threshold=goal.threshold if goal.threshold else 0.005
                )
                self.event.clear()
                self.j_v = np.linalg.pinv(
                    self.jacobe(self.q)) @ velocities
                self.last_update = timeit.default_timer()
                self.event.wait()

            self.moving = False
            result = not self.preempted
            self.preempted = False

            # self.qd *= 0
            if result:
                self.pose_servo_server.set_succeeded(
                    ServoToPoseResult(success=True)
                )
            else:
                self.pose_servo_server.set_aborted(
                    ServoToPoseResult(success=False)
                )

    def joint_pose_cb(self, goal: MoveToJointPoseGoal) -> None:
        """
        ROS Action Server callback:
        Moves the arm the named pose indicated by goal

        :param goal: Goal message containing the name of
        the joint configuration to which the arm should move
        :type goal: MoveToNamedPoseGoal
        """
        if self.moving:
            self.preempt()

        with self.lock:
            traj = rtb.tools.trajectory.jtraj(
                self.q,
                np.array(goal.joints),
                self.frequency,
            )

            if self.__traj_move(traj, goal.speed if goal.speed else 0.2):
                self.named_pose_server.set_succeeded(
                    MoveToJointPoseResult(success=True)
                )
            else:
                self.joint_pose_server.set_aborted(
                    MoveToJointPoseResult(success=False)
                )

    def named_pose_cb(self, goal: MoveToNamedPoseGoal) -> None:
        """
        ROS Action Server callback:
        Moves the arm the named pose indicated by goal

        :param goal: Goal message containing the name of
        the joint configuration to which the arm should move
        :type goal: MoveToNamedPoseGoal
        """
        if self.moving:
            self.preempt()

        with self.lock:
            if not goal.pose_name in self.named_poses:
                self.named_pose_server.set_aborted(
                    MoveToNamedPoseResult(success=False),
                    'Unknown named pose'
                )

            traj = rtb.tools.trajectory.jtraj(
                self.q,
                np.array(self.named_poses[goal.pose_name]),
                self.frequency
            )

            if self.__traj_move(traj, goal.speed if goal.speed else 0.2):
                self.named_pose_server.set_succeeded(
                    MoveToNamedPoseResult(success=True)
                )
            else:
                self.named_pose_server.set_aborted(
                    MoveToNamedPoseResult(success=False)
                )

    def home_cb(self, req: EmptyRequest) -> EmptyResponse:
        """[summary]

        :param req: Empty request
        :type req: EmptyRequest
        :return: Empty response
        :rtype: EmptyResponse
        """
        if self.moving:
            self.preempt()

        with self.lock:
            traj = rtb.tools.trajectory.jtraj(
                self.q,
                self.qr if hasattr(self, 'qr') else self.q, # pylint: disable=no-member
                self.frequency
            )
            self.__traj_move(traj, max_speed=0.2)
            return EmptyResponse()

    def recover_cb(self, req: EmptyRequest) -> EmptyResponse: # pylint: disable=no-self-use
        """[summary]
        ROS Service callback:
        Invoke any available error recovery functions on the robot when an error occurs

        :param req: an empty request
        :type req: EmptyRequest
        :return: an empty response
        :rtype: EmptyResponse
        """
        rospy.logwarn('Recovery not implemented for this arm')
        return EmptyResponse()

    def set_cartesian_impedance_cb(  # pylint: disable=no-self-use
            self,
            request: SetCartesianImpedanceRequest) -> SetCartesianImpedanceResponse:
        """
        ROS Service Callback
        Set the 6-DOF impedance of the end-effector. Higher values should increase the stiffness
        of the robot while lower values should increase compliance

        :param request: The numeric values representing the EE impedance (6-DOF) that
        should be set on the arm
        :type request: GetNamedPoseConfigsRequest
        :return: True if the impedence values were updated successfully
        :rtype: GetNamedPoseConfigsResponse
        """
        rospy.logwarn(
            'Setting cartesian impedance not implemented for this arm')
        return SetCartesianImpedanceResponse(True)

    def preempt(self, *args: list) -> None:
        """
        Stops any current motion
        """
        # pylint: disable=unused-argument
        self.preempted = True
        self.e_v *= 0
        self.j_v *= 0
        self.qd *= 0

    def __vel_move(self, twist_stamped: TwistStamped) -> None:
        target: Twist = twist_stamped.twist

        if twist_stamped.header.frame_id and twist_stamped.header.frame_id != self.base_link.name:
            self.e_v_frame = twist_stamped.header.frame_id
        else:
            self.e_v_frame = None

        e_v = np.array([
            target.linear.x,
            target.linear.y,
            target.linear.z,
            target.angular.x,
            target.angular.y,
            target.angular.z
        ])

        if np.any(e_v - self.e_v):
            self.e_p = self.fkine(self.q, start=self.base_link, fast=True, end=self.gripper)

        self.e_v = e_v

        self.last_update = timeit.default_timer()

    def __traj_move(self, traj: np.array, max_speed=0.2) -> bool:
        """[summary]

        :param traj: [description]
        :type traj: np.array
        :return: [description]
        :rtype: bool
        """
        self.moving = True

        t = 0
        delta = 1/self.frequency
        
        qfunc = interp1d(np.linspace(0, 1, traj.q.shape[0]), traj.q, axis=0)

        #Debugging Prints
        print(f"Traj_Move Prints:\n\tDelta: {delta}\n\tMax Speed: {max_speed}")

        # ------- TEST NEW Method -------------------------------------------------------
        av_vel = 0.025 #max_speed
        frequency = self.frequency
        end_point = qfunc(1)
        beg_point = self.q
        # This time is arbitrary, but we pick the maximum in order to reduce the joint velocities
        total_time_cal = np.max(np.fabs((end_point - beg_point) / av_vel))
        time_freq = int(total_time_cal * frequency)

        # Updated by Dasun: reduced gain for better control
        # This is a very large gain (not too sure why) that allows the arm to reach the goal 
        # This should also be a function of time (smaller gain for shorter distances)
        kP = 1500 # * total_time_cal

        print(f"max total time: {total_time_cal}")
        print(f"beg joint positions: {beg_point}")
        print(f"end joint positions: {end_point}")
        time = 1
        # For debugging
        twist_speed_vect = []
        while t + delta < total_time_cal and not self.preempted:

            # Check if we are close to goal state as an exit point
            if np.all(np.fabs(qfunc(1) - self.q) < 0.02):
                print('Too close to goal, quitting movement...')
                break
            
            # Calculate the joint velocity required in this step 
            # Note this calculates a velocity profile from a minimum jerk traj 
            # (based on https://mika-s.github.io/python/control-theory/trajectory-generation/2017/12/06/trajectory-generation-with-a-minimum-jerk-trajectory.html)
            jV = frequency * (1.0/time_freq) * (end_point - self.q) * (30.0 * (time/time_freq)**2.0
                - 60.0 * (time/time_freq)**3.0
                + 30.0 * (time/time_freq)**4.0)

            # Minimum jerk trajectory calculation of expected position (Used for Debugging)
            pos = beg_point + (end_point - beg_point) * (10.0 * (time/time_freq)**3
             - 15.0 * (time/time_freq)**4
             + 6.0 * (time/time_freq)**5)
            
            #print(f"time step: {time}")
            #print(f"jV calculated at {t}: {jV}")
            #print(f"pos at {t}: {self.q}")
            #print(f"pos calculated at {t}: {pos}")

            # Added by Dasun: gain required to have smooth control on panda
            jV = jV * kP
            #print(f"jV corrected at {t}: {jV}")

            jacob0 = self.jacob0(self.q, fast=True, end=self.gripper)
            twist = jacob0 @ jV
            #print(f"twist (after jacobian) at {t}: {twist}")
            linear_vel = np.linalg.norm(twist[:3])

            # Scale the twist velocity based on max_speed
            time_scaling = 1
            if linear_vel > max_speed:
                normalised_vel = (twist / linear_vel) * max_speed
                time_scaling = np.linalg.norm(normalised_vel) / linear_vel    
                linear_vel = linear_vel * time_scaling 
            
            twist_speed_vect.append(linear_vel)
            print(f"linear vel at {t}: {linear_vel}\n\n")
                
            t += delta * time_scaling
            
            self.event.clear()
            self.j_v = jV * time_scaling
            self.last_update = timeit.default_timer()
            self.event.wait()
            time+=1
        
        ### ----------- Original Method Commented Out For Now ------------------------
        # while t + delta < 1 and not self.preempted:

        #     # Check if we are close to goal state as an exit point
        #     if np.all(np.fabs(qfunc(1) - self.q) < 0.005):
        #         print('Too close to goal, quitting movement...')
        #         break
            
        #     # Calculate the joint velocity required in this step based on the 
        #     # current joint states (self.q) and the expected next state qfunc(t+delta)
        #     jV = (qfunc(t + delta) - self.q) / delta #self.q) / delta
            
        #     #print(f"count: {count}")
        #     print(f"jV calculated at {t}: {jV}")
        #     print(f"pos at {t}: {self.q}")
        #     print(f"qfunc pos at {t}: {qfunc(t)}")
        #     # Added by Dasun: gain required to have smooth control on panda
        #     jV = jV * 0.05

        #     jacob0 = self.jacob0(self.q, fast=True, end=self.gripper)

        #     twist = jacob0 @ jV
        #     print(f"twist (after jacobian) at {t}: {twist}")
        #     linear_vel = np.linalg.norm(twist[:3])
        #     print(f"linear_vel (norm twist) at {t}: {linear_vel}\n\n")

        #     time_scaling = 1

        #     if linear_vel > max_speed:
        #         normalised_vel = (twist / linear_vel) * max_speed
        #         time_scaling = np.linalg.norm(normalised_vel) / linear_vel
                
        #     t += delta * time_scaling
            
        #     self.event.clear()
        #     self.j_v = jV * time_scaling
        #     self.last_update = timeit.default_timer()
        #     self.event.wait()

        # Print of maximum twist velocity
        if twist_speed_vect:
            print(f"Max twist velocity: {np.max(twist_speed_vect)}")
            print(f"Average twist velocity: {np.average(twist_speed_vect)}")
            #print(twist_speed_vect)

        self.j_v = [0] * self.n

        self.moving = False
        result = not self.preempted
        self.preempted = False
        return result

    def get_state(self) -> ManipulatorState:
        """
        Generates a ManipulatorState message for the robot

        :return: ManipulatorState message describing the current state of the robot
        :rtype: ManipulatorState
        """
        jacob0 = self.jacob0(self.q, fast=True, end=self.gripper)
        
        ## end-effector position
        ee_pose = self.fkine(self.q, start=self.base_link, end=self.gripper, fast=True)
        header = Header()
        header.frame_id = self.base_link.name
        header.stamp = rospy.Time.now()

        pose_stamped = PoseStamped()
        pose_stamped.header = header

        translation = ee_pose[:3, 3]
        pose_stamped.pose.position.x = translation[0]
        pose_stamped.pose.position.y = translation[1]
        pose_stamped.pose.position.z = translation[2]

        rotation = ee_pose[:3, :3]
        ee_rot = sp.UnitQuaternion(rotation)

        pose_stamped.pose.orientation.w = ee_rot.A[0]
        pose_stamped.pose.orientation.x = ee_rot.A[1]
        pose_stamped.pose.orientation.y = ee_rot.A[2]
        pose_stamped.pose.orientation.z = ee_rot.A[3]

        state = ManipulatorState()
        state.ee_pose = pose_stamped

        # end-effector velocity
        T = jacob0 @ self.qd

        twist_stamped = TwistStamped()
        twist_stamped.header = header
        twist_stamped.twist.linear.x = T[0]
        twist_stamped.twist.linear.y = T[1]
        twist_stamped.twist.linear.z = T[2]
        twist_stamped.twist.angular.x = T[3]
        twist_stamped.twist.angular.x = T[4]
        twist_stamped.twist.angular.x = T[5]

        state.ee_velocity = twist_stamped
        
        # joints
        if self.joint_states:
            state.joint_poses = np.array(self.joint_states.position)[self.joint_indexes]
            state.joint_velocities = np.array(self.joint_states.velocity)[self.joint_indexes]
            state.joint_torques = np.array(self.joint_states.effort)[self.joint_indexes]
        
        else:
            state.joint_poses = list(self.q)
            state.joint_velocities = list(self.qd)
            state.joint_torques = [0] * self.n
        
        return state

    def test_guards(
        self,
        guards: Guards,
        start_time: float) -> int:

        triggered = 0

        if (guards.enabled & guards.GUARD_DURATION) == guards.GUARD_DURATION:
            triggered |= guards.GUARD_DURATION if timeit.default_timer() - start_time > guards.duration else 0

        if (guards.enabled & guards.GUARD_EFFORT) == guards.GUARD_EFFORT:
            eActual = np.fabs(np.array([
                self.state.ee_wrench.wrench.force.x,
                self.state.ee_wrench.wrench.force.y,
                self.state.ee_wrench.wrench.force.z,
                self.state.ee_wrench.wrench.torque.x,
                self.state.ee_wrench.wrench.torque.y,
                self.state.ee_wrench.wrench.torque.z,
            ]))

            eThreshold = np.array([
                guards.effort.force.x,
                guards.effort.force.y,
                guards.effort.force.z,
                guards.effort.torque.x,
                guards.effort.torque.y,
                guards.effort.torque.z,
            ])

            triggered |= guards.GUARD_EFFORT if np.any(eActual > eThreshold) else 0
            
        return triggered

    def get_named_poses_cb(self, req: GetNamedPosesRequest) -> GetNamedPosesResponse:
        """
        ROS Service callback:
        Retrieves the list of named poses available to the arm

        :param req: An empty request
        :type req: GetNamesListRequest
        :return: The list of named poses available for the arm
        :rtype: GetNamesListResponse
        """
        return GetNamedPosesResponse(list(self.named_poses.keys()))

    def add_named_pose_cb(self, req: AddNamedPoseRequest) -> AddNamedPoseResponse:
        """
        ROS Service callback:
        Adds the current arm pose as a named pose and saves it to the host config

        :param req: The name of the pose as well as whether to overwrite if the pose already exists
        :type req: AddNamedPoseRequest
        :return: True if the named pose was written successfully otherwise false
        :rtype: AddNamedPoseResponse
        """
        if req.pose_name in self.named_poses and not req.overwrite:
            rospy.logerr('Named pose already exists.')
            return AddNamedPoseResponse(success=False)

        self.named_poses[req.pose_name] = self.q.tolist()
        self.__write_config('named_poses', self.named_poses)

        return AddNamedPoseResponse(success=True)

    def remove_named_pose_cb(self, req: RemoveNamedPoseRequest) -> RemoveNamedPoseResponse:
        """
        ROS Service callback:
        Adds the current arm pose as a named pose and saves it to the host config

        :param req: The name of the pose as well as whether to overwrite if the pose already exists
        :type req: AddNamedPoseRequest
        :return: True if the named pose was written successfully otherwise false
        :rtype: AddNamedPoseResponse
        """
        if req.pose_name not in self.named_poses and not req.overwrite:
            rospy.logerr('Named pose does not exists.')
            return AddNamedPoseResponse(success=False)

        del self.named_poses[req.pose_name]
        self.__write_config('named_poses', self.named_poses)

        return AddNamedPoseResponse(success=True)

    def add_named_pose_config_cb(
            self,
            request: AddNamedPoseConfigRequest) -> AddNamedPoseConfigResponse:
        """[summary]

        :param request: [description]
        :type request: AddNamedPoseConfigRequest
        :return: [description]
        :rtype: AddNamedPoseConfigResponse
        """
        self.custom_configs.append(request.config_path)
        self.__load_config()
        return True

    def remove_named_pose_config_cb(
            self,
            request: RemoveNamedPoseConfigRequest) -> RemoveNamedPoseConfigResponse:
        """[summary]

        :param request: [description]
        :type request: AddNamedPoseRequest
        :return: [description]
        :rtype: [type]
        """
        if request.config_path in self.custom_configs:
            self.custom_configs.remove(request.config_path)
            self.__load_config()
        return True

    def get_named_pose_configs_cb(
            self,
            request: GetNamedPoseConfigsRequest) -> GetNamedPoseConfigsResponse:
        """[summary]

        :param request: [description]
        :type request: GetNamedPoseConfigsRequest
        :return: [description]
        :rtype: GetNamedPoseConfigsResponse
        """
        return self.custom_configs

    def publish(self):
        self.joint_publisher.publish(Float64MultiArray(data=self.qd))

    def step(self, dt: float = 0.01) -> None:  # pylint: disable=unused-argument
        """
        Updates the robot joints (robot.q) used in computing kinematics
        :param dt: the delta time since the last update, defaults to 0.01
        :type dt: float, optional
        """
        if self.readonly:
            return

        current_time = timeit.default_timer()
        self.state = self.get_state()

        # calculate joint velocities from desired cartesian velocity
        if any(self.e_v):
            if current_time - self.last_update > 0.1:
                self.e_v *= 0.9 if np.sum(np.absolute(self.e_v)
                                          ) >= 0.0001 else 0

            ## --------- DEBUG: this is the new section that has issues ------
            # p = self.e_p[:3, 3] + self.e_v[:3] * dt
            # R = SO3(self.e_p[:3, :3]) * SO3.EulerVec(self.e_v[3:] * dt)
            
            # T = SE3.Rt(R, p)

            # Tactual = SE3(self.fkine(self.q, start=self.base_link, fast=True, end=self.gripper))
            # T = SE3.Rt(R, p)

            # delta = Tactual.delta(T)
            # e_v = self.e_v + delta
            ## --------- DEBUG: end of new section with issues --------

            ## --------- DEBUG: Old stuff start [commit 3272b27] WORKING on PANDA
            Kp = 1 
            wTe = self.fkine(self.q, start=self.base_link, fast=True, end=self.gripper)
            error = self.e_p @ np.linalg.inv(wTe)
            # print(e)
            trans = self.e_p[:3, 3]  # .astype('float64')
            trans += self.e_v[:3] * dt
            rotation = SO3(self.e_p[:3, :3])

            # Rdelta = SO3.EulerVec(self.e_v[3:])

            # R = Rdelta * R
            # R = R.norm()
            # # print(self.e_p)
            self.e_p = SE3.Rt(rotation, t=trans).A

            v_t = self.e_v[:3] + Kp * error[:3, 3]
            v_r = self.e_v[3:]  # + (e[.rpy(]) * 0.5)

            e_v = np.concatenate([v_t, v_r])
            ## -------- DEBUG: old stuff end ---------------------------
            
            self.j_v = np.linalg.pinv(
                self.jacob0(self.q, fast=True, end=self.gripper)) @ e_v

            ## ---- DEBUG: part of current version commented out
            #self.e_p = SE3.Rt(R, t=p).A

        # apply desired joint velocity to robot
        if any(self.j_v):
            if current_time - self.last_update > 0.1:
                self.j_v *= 0.9 if np.sum(np.absolute(self.j_v)
                                          ) >= 0.0001 else 0

        self.qd = self.j_v
        self.last_tick = current_time

        self.state_publisher.publish(self.state)

        self.event.set()

    def __load_config(self):
        """[summary]
        """
        self.named_poses = {}
        for config_name in self.custom_configs:
            try:
                config = yaml.load(open(config_name))
                if config and 'named_poses' in config:
                    self.named_poses.update(config['named_poses'])
            except IOError:
                rospy.logwarn(
                    'Unable to locate configuration file: {}'.format(config_name))

        if os.path.exists(self.config_path):
            try:
                config = yaml.load(open(self.config_path),
                                   Loader=yaml.SafeLoader)
                if config and 'named_poses' in config:
                    self.named_poses.update(config['named_poses'])

            except IOError:
                pass

    def __write_config(self, key: str, value: Any):
        """[summary]

        :param key: [description]
        :type key: str
        :param value: [description]
        :type value: Any
        """
        if not os.path.exists(os.path.dirname(self.config_path)):
            os.makedirs(os.path.dirname(self.config_path))

        config = {}

        try:
            with open(self.config_path) as handle:
                current = yaml.load(handle.read())

                if current:
                    config = current

        except IOError:
            pass

        config.update({key: value})

        with open(self.config_path, 'w') as handle:
            handle.write(yaml.dump(config))
