import json
import logging

logger = logging.getLogger('spacel.provision.template.app_spot')

ASG_RESOURCES = ('Asg',
                 'Lc')

GDH_ASG_RESOURCE = ('SpScaleDown',
                    'SpScaleUp',
                    'AlarmScaleDown',
                    'AlarmScaleUp',
                    'AlarmContinuousHighLoad')


class AppSpotTemplateDecorator(object):
    def spotify(self, app_region, template):
        """
        Replace ASG with SpotFleet. Literally spot-ify.
        :return:
        """
        if app_region.spot is None:
            return

        resources = template['Resources']
        parameters = template['Parameters']
        self._add_spot_fleet(app_region, resources, parameters)
        self._clean_up_asg(template)

    @staticmethod
    def _add_spot_fleet(app_region, resources, parameters):
        spot_price = app_region.spot.get('price', '1.00')

        if parameters.get('UserData') is not None:
            # Set Name tag
            tags = {'Name': app_region.app.full_name}
            user_data_param = parameters['UserData']['Default'] or ''
            if user_data_param:
                user_data_param += ','
            user_data_param += '"tags":' + json.dumps(tags)
            parameters['UserData']['Default'] = user_data_param

        # Extract parameters:
        lc_properties = resources['Lc']['Properties']
        user_data = lc_properties['UserData']
        block_mapping = lc_properties['BlockDeviceMappings']
        monitoring = {'Enabled': lc_properties['InstanceMonitoring']}
        image_id = lc_properties['ImageId']
        instance_profile = {
            'Arn': {
                'Fn::GetAtt': [lc_properties['IamInstanceProfile']['Ref'],
                               'Arn']
            }
        }
        security_groups = [
            {'GroupId': {'Fn::GetAtt': [sg['Ref'], 'GroupId']}}
            for sg in lc_properties['SecurityGroups']]

        subnets = resources['Asg']['Properties']['VPCZoneIdentifier']

        # Instance weights can be specified
        weights = app_region.spot.get('weights')
        if not weights:
            weights = {app_region.instance_type: 1}

        # If we're bidding on a single instance type, prefer AZ saturation
        # else prefer lowest price.
        if len(weights) == 1:
            default_allocation_strat = 'diversified'
        else:
            default_allocation_strat = 'lowestPrice'
        allocation_strat = app_region.spot.get('strategy',
                                               default_allocation_strat)

        # Build launch specs:
        launch_specs = []
        for weight_type, weight in weights.items():
            for subnet_id in subnets:
                launch_specs.append({
                    'UserData': user_data,
                    'BlockDeviceMappings': block_mapping,
                    'IamInstanceProfile': instance_profile,
                    'InstanceType': weight_type,
                    'ImageId': image_id,
                    'Monitoring': monitoring,
                    'SecurityGroups': security_groups,
                    'WeightedCapacity': weight,
                    'SubnetId': subnet_id
                })
        logger.debug('Generated %d launch specs.', len(launch_specs))

        # Add spotfleet resource:
        resources['SpotFleet'] = {
            'Type': 'AWS::EC2::SpotFleet',
            'Properties': {
                'SpotFleetRequestConfigData': {
                    'AllocationStrategy': allocation_strat,
                    'IamFleetRole': app_region.orbit_region.spot_fleet_role,
                    'SpotPrice': spot_price,
                    'TargetCapacity': {'Ref': 'InstanceMin'},
                    'TerminateInstancesWithExpiration': 'true',
                    'LaunchSpecifications': launch_specs
                }
            }
        }

    @staticmethod
    def _clean_up_asg(template):
        resources = template['Resources']

        # Remove any resources that reference the Asg/Lc:
        for resource in ASG_RESOURCES:
            resources.pop(resource, None)
        for gdh_resource in GDH_ASG_RESOURCE:
            resources.pop(gdh_resource, None)

        # Redirect any outputs that point at the Asg/Lc:
        outputs = template.get('Outputs', {})
        for _, output_params in outputs.items():
            output_ref = output_params.get('Value', {}).get('Ref')
            if output_ref == 'Asg':
                output_params['Value']['Ref'] = 'SpotFleet'
        asg_name = outputs.get('AsgName')
        if asg_name:
            asg_name['Value']['Ref'] = 'SpotFleet'
