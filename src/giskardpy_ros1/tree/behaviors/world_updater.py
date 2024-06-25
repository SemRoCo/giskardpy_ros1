import traceback
from copy import deepcopy
from threading import Thread

import rospy
from py_trees import Status
from visualization_msgs.msg import MarkerArray

from giskard_msgs.msg import WorldResult, WorldGoal
from giskard_msgs.srv import GetGroupNamesResponse, GetGroupNamesRequest, GetGroupInfoResponse, GetGroupInfoRequest, \
    DyeGroupResponse, GetGroupNames, GetGroupInfo, DyeGroup, DyeGroupRequest
from giskardpy.data_types.data_types import JointStates, PrefixName
from giskardpy.data_types.exceptions import UnknownGroupException, \
    TransformException, DuplicateNameException, InvalidWorldOperationException
from giskardpy.god_map import god_map
from giskardpy.model.joints import Joint6DOF
from giskardpy.model.world import WorldBranch
from giskardpy_ros1.tree.behaviors.action_server import ActionServerHandler
from giskardpy_ros1.tree.behaviors.plugin import GiskardBehavior
from giskardpy.middleware import logging
from giskardpy_ros1.tree.blackboard_utils import GiskardBlackboard
from giskardpy.utils.decorators import record_time
from giskardpy.middleware.ros1.tfwrapper import transform_pose
import giskardpy.middleware.ros1.msg_converter as msg_converter


class ProcessWorldUpdate(GiskardBehavior):

    def __init__(self, action_server: ActionServerHandler):
        self.action_server = action_server
        name = f'Processing \'{self.action_server.name}\''
        self.started = False
        super().__init__(name)

    @record_time
    @profile
    def setup(self, timeout: float = 5.0):
        self.marker_publisher = rospy.Publisher('~visualization_marker_array', MarkerArray, queue_size=1)
        self.get_group_names = rospy.Service('~get_group_names', GetGroupNames, self.get_group_names_cb)
        self.get_group_info = rospy.Service('~get_group_info', GetGroupInfo, self.get_group_info_cb)
        self.dye_group = rospy.Service('~dye_group', DyeGroup, self.dye_group)
        return super().setup(timeout)

    def update(self) -> Status:
        if not self.started:
            logging.loginfo(f'Processing world goal #{GiskardBlackboard().world_action_server.goal_id}.')
            self.worker_thread = Thread(target=self.process_goal, name=self.name)
            self.worker_thread.start()
            self.started = True
        if self.worker_thread.is_alive():
            return Status.RUNNING
        self.started = False
        logging.loginfo(f'Finished world goal #{GiskardBlackboard().world_action_server.goal_id}.')
        return Status.SUCCESS

    def process_goal(self):
        req = self.action_server.goal_msg
        result = WorldResult()
        try:
            if req.operation == WorldGoal.ADD:
                self.add_object(req)
            elif req.operation == WorldGoal.UPDATE_PARENT_LINK:
                self.update_parent_link(req)
            elif req.operation == WorldGoal.UPDATE_POSE:
                self.update_group_pose(req)
            elif req.operation == WorldGoal.REGISTER_GROUP:
                self.register_group(req)
            elif req.operation == WorldGoal.REMOVE:
                self.remove_object(req.group_name)
            elif req.operation == WorldGoal.REMOVE_ALL:
                self.clear_world()
            else:
                raise InvalidWorldOperationException(f'Received invalid operation code: {req.operation}')
        except Exception as e:
            traceback.print_exc()
            result.error = msg_converter.exception_to_error_msg(e)
        self.action_server.result_msg = result

    def dye_group(self, req: DyeGroupRequest):
        res = DyeGroupResponse()
        try:
            god_map.world.dye_group(req.group_name, req.color)
            res.error_codes = DyeGroupResponse.SUCCESS
            logging.loginfo(
                f'dyed group \'{req.group_name}\' to r:{req.color.r} g:{req.color.g} b:{req.color.b} a:{req.color.a}')
        except UnknownGroupException:
            res.error_codes = DyeGroupResponse.GROUP_NOT_FOUND_ERROR
        return res

    @profile
    def get_group_names_cb(self, req: GetGroupNamesRequest) -> GetGroupNamesResponse:
        group_names = god_map.world.group_names
        res = GetGroupNamesResponse()
        res.group_names = list(sorted(group_names))
        return res

    @profile
    def get_group_info_cb(self, req: GetGroupInfoRequest) -> GetGroupInfoResponse:
        res = GetGroupInfoResponse()
        res.error_codes = GetGroupInfoResponse.SUCCESS
        try:
            group = god_map.world.groups[req.group_name]  # type: WorldBranch
            res.controlled_joints = [str(j.short_name) for j in group.controlled_joints]
            res.links = list(sorted(str(x.short_name) for x in group.link_names_as_set))
            res.child_groups = list(sorted(str(x) for x in group.groups.keys()))
            res.root_link_pose = msg_converter.trans_matrix_to_pose_stamped(group.base_pose)
            for key, value in group.state.items():
                res.joint_state.name.append(str(key))
                res.joint_state.position.append(value.position)
                res.joint_state.velocity.append(value.velocity)
        except KeyError as e:
            logging.logerr(f'no object with the name {req.group_name} was found')
            res.error_codes = GetGroupInfoResponse.GROUP_NOT_FOUND_ERROR

        return res

    @profile
    def add_object(self, req: WorldGoal) -> None:
        group_name = req.group_name
        if group_name in god_map.world.groups:
            raise DuplicateNameException(f'Group with name \'{req.group_name}\' already exists.')
        parent_link = msg_converter.link_name_msg_to_prefix_name(req.parent_link, god_map.world)
        world_body = req.body
        pose = req.pose

        if req.pose.header.frame_id == '':
            raise TransformException('Frame_id in pose is not set.')
        try:
            # first try to transform to map using tf to deal with time stamps
            pose = transform_pose(target_frame=god_map.world.root_link_name, pose=req.pose, timeout=0.5)
        except:
            # tf is not available, just ignore this step
            pass
        with god_map.world.modify_world() as world:
            pose = msg_converter.pose_stamped_to_trans_matrix(pose, world)
            parent_link_T_group_root_link = world.transform(parent_link, pose)
            link_name = PrefixName(group_name, group_name)
            if world_body.type == world_body.URDF_BODY:
                world.add_urdf(urdf=world_body.urdf,
                               parent_link_name=parent_link,
                               group_name=group_name,
                               pose=parent_link_T_group_root_link)
            else:
                link = msg_converter.world_body_to_link(link_name=link_name,
                                                        msg=world_body,
                                                        color=god_map.world.default_link_color)
                joint = Joint6DOF(name=PrefixName(group_name, god_map.world.connection_prefix),
                                  parent_link_name=parent_link,
                                  child_link_name=link.name)
                joint.update_transform(parent_link_T_group_root_link)
                world.add_link(link)
                world.add_joint(joint)
                world.register_group(group_name, link.name)
        # SUB-CASE: If it is an articulated object, open up a joint state subscriber
        logging.loginfo(f'Attached object \'{group_name}\' at \'{parent_link}\'.')
        if world_body.joint_state_topic:
            GiskardBlackboard().tree.wait_for_goal.synchronization.sync_joint_state_topic(
                group_name=group_name,
                topic_name=world_body.joint_state_topic)
        # FIXME also keep track of base pose
        if world_body.tf_root_link_name:
            raise NotImplementedError('tf_root_link_name is not implemented')

    @profile
    def update_group_pose(self, req: WorldGoal):
        if req.group_name not in god_map.world.groups:
            raise UnknownGroupException(f'Can\'t update pose of unknown group: \'{req.group_name}\'')
        group = god_map.world.groups[req.group_name]
        joint_name = group.root_link.parent_joint_name
        pose = msg_converter.ros_msg_to_giskard_obj(req.pose, god_map.world)
        pose = god_map.world.transform(god_map.world.joints[joint_name].parent_link_name, pose)
        god_map.world.joints[joint_name].update_transform(pose)
        god_map.world.notify_state_change()

    @profile
    def update_parent_link(self, req: WorldGoal):
        parent_link = msg_converter.link_name_msg_to_prefix_name(req.parent_link, god_map.world)
        if req.group_name not in god_map.world.groups:
            raise UnknownGroupException(f'Can\'t attach to unknown group: \'{req.group_name}\'')
        group = god_map.world.groups[req.group_name]
        if group.root_link_name != parent_link:
            old_parent_link = group.parent_link_of_root
            god_map.world.move_group(req.group_name, parent_link)
            logging.loginfo(f'Reattached \'{req.group_name}\' from \'{old_parent_link}\' to \'{req.parent_link}\'.')
        else:
            logging.logwarn(f'Didn\'t update world. \'{req.group_name}\' is already attached to \'{req.parent_link}\'.')

    @profile
    def remove_object(self, name: str):
        if name not in god_map.world.groups:
            raise UnknownGroupException(f'Can not remove unknown group: {name}.')
        god_map.world.delete_group(name)
        GiskardBlackboard().tree.wait_for_goal.synchronization.remove_group_behaviors(name)
        logging.loginfo(f'Deleted \'{name}\'.')

    @profile
    def clear_world(self):
        tmp_state = deepcopy(god_map.world.state)
        god_map.world.clear()
        with god_map.world.modify_world():
            GiskardBlackboard().giskard.world_config.setup()
        GiskardBlackboard().tree.wait_for_goal.synchronization.remove_added_behaviors()
        # copy only state of joints that didn't get deleted
        remaining_free_variables = list(god_map.world.free_variables.keys()) + list(
            god_map.world.virtual_free_variables.keys())
        god_map.world.state = JointStates({k: v for k, v in tmp_state.items() if k in remaining_free_variables})
        god_map.world.notify_state_change()
        god_map.collision_scene.sync()
        GiskardBlackboard().giskard.collision_avoidance_config.setup()
        # self.clear_markers()
        logging.loginfo('Cleared world.')

    def register_group(self, req: WorldGoal):
        link_name = msg_converter.link_name_msg_to_prefix_name(req.parent_link, god_map.world)
        god_map.world.register_group(name=req.group_name, root_link_name=link_name)
        logging.loginfo(f'Registered new group \'{req.group_name}\'')
