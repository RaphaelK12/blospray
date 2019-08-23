# ======================================================================== #
# BLOSPRAY - OSPRay as a Blender render engine                             #
# Paul Melis, SURFsara <paul.melis@surfsara.nl>                            #
# Connection to render server, scene export, result handling               #
# ======================================================================== #
# Copyright 2018-2019 SURFsara                                             #
#                                                                          #
# Licensed under the Apache License, Version 2.0 (the "License");          #
# you may not use this file except in compliance with the License.         #
# You may obtain a copy of the License at                                  #
#                                                                          #
#     http://www.apache.org/licenses/LICENSE-2.0                           #
#                                                                          #
# Unless required by applicable law or agreed to in writing, software      #
# distributed under the License is distributed on an "AS IS" BASIS,        #
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. #
# See the License for the specific language governing permissions and      #
# limitations under the License.                                           #
# ======================================================================== #

# - Make sockets non-blocking and use select() to handle errors on the server side

import bpy, bmesh
#from bgl import *
from mathutils import Vector

import sys, array, json, os, select, socket, time
from math import tan, atan, degrees, radians
from struct import pack, unpack

import numpy

sys.path.insert(0, os.path.split(__file__)[0])

from .common import send_protobuf, receive_protobuf
from .messages_pb2 import (
    CameraSettings, ImageSettings,
    LightSettings, Light, RenderSettings,
    SceneElement, MeshData,
    ClientMessage, GenerateFunctionResult,
    RenderResult,
    UpdateObject, UpdatePluginInstance
)

# Object to world matrix
#
# Matrix(((0.013929054141044617, 0.0, 0.0, -0.8794544339179993),
#         (0.0, 0.013929054141044617, 0.0, -0.8227154612541199),
#         (0.0, 0.0, 0.013929054141044617, 0.0),
#         (0.0, 0.0, 0.0, 1.0)))
#
# Translation part is in right-most column

def matrix2list(m):
    """Convert to list of 16 floats"""
    values = []
    for row in m:
        values.extend(list(row))
    return values

def customproperties2dict(obj, filepath_keys=['file']):
    user_keys = [k for k in obj.keys() if k not in ['_RNA_UI', 'ospray']]
    properties = {}
    for k in user_keys:
        v = obj[k]
        if hasattr(v, 'to_dict'):
            properties[k] = v.to_dict()
        elif hasattr(v, 'to_list'):
            properties[k] = v.to_list()
        else:
            # XXX assumes simple type that can be serialized to json
            properties[k] = v

    for k in filepath_keys:
        if k in properties:
            # Convert blendfile-relative paths to full paths, e.g.
            # //.../file.name -> /.../.../file.name
            properties[k] = bpy.path.abspath(properties[k])

    return properties


class Connection:

    def __init__(self, engine, host, port):
        self.engine = engine

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        self.host = host
        self.port = port

        self.framebuffer_width = self.framebuffer_height = None

    def close(self):
        # XXX send bye
        self.sock.close()

    def update(self, data, depsgraph):
        print(data)
        print(depsgraph)

        self.engine.update_stats('', 'Connecting')

        self.sock.connect((self.host, self.port))

        # Send HELLO and check returned version
        #hello = ClientMessage()
        #hello.type = ClientMessage.HELLO
        #hello.version = 1
        #send_protobuf(self.sock, hello)

        self.mesh_data_exported = set()
        
        self.export_scene(data, depsgraph)

    #
    # Scene export
    #

    def _process_properties(self, obj, properties):
        """
        Get Blender custom properties set on obj.
        Custom properties starting with an underscore become
        element properties (key without the underscore), all the others
        become plugin parameters, set in the key "plugin_parameters",
        *but only if a property key 'plugin_type' is set*.
        """

        plugin_parameters = None
        if 'plugin_type' in properties:
            if 'plugin_parameters' in properties:
                plugin_parameters = properties['plugin_parameters']
            else:
                plugin_parameters = properties['plugin_parameters'] = {}

        custom_properties = customproperties2dict(obj)
        for k, v in custom_properties.items():
            #print('k', k, 'v', v)
            if k[0] == '_':
                # This might overwrite one of the already defined properties above
                #print(properties, k, v)
                properties[k[1:]] = v
            elif plugin_parameters is not None:
                plugin_parameters[k] = v
                
    def _process_properties2(self, obj, extract_plugin_parameters=False):
        """
        Get Blender custom properties set on obj.
        Custom properties starting with an underscore become
        element properties (with a key without the underscore), 
        all the others become plugin parameters, but only if
        extract_plugin_parameters is True.
        """

        properties = {}
        plugin_parameters = {}

        for k, v in customproperties2dict(obj).items():
            #print('k', k, 'v', v)
            if k[0] == '_':
                #print(properties, k, v)
                properties[k[1:]] = v
            elif extract_plugin_parameters:
                plugin_parameters[k] = v       
            else:
                properties[k] = v
                
        return properties, plugin_parameters

    def export_scene(self, data, depsgraph):

        msg = 'Exporting scene'
        self.engine.update_stats('', msg)
        print(msg)

        client_message = ClientMessage()
        client_message.type = ClientMessage.UPDATE_SCENE
        send_protobuf(self.sock, client_message)

        scene = depsgraph.scene
        render = scene.render
        world = scene.world

        scale = render.resolution_percentage / 100.0
        self.framebuffer_width = int(render.resolution_x * scale)
        self.framebuffer_height = int(render.resolution_y * scale)

        aspect = self.framebuffer_width / self.framebuffer_height

        print("%d x %d (scale %d%%) -> %d x %d (aspect %.3f)" % \
            (render.resolution_x, render.resolution_y, render.resolution_percentage,
            self.framebuffer_width, self.framebuffer_height, aspect))

        # Image

        image_settings = ImageSettings()

        if render.use_border:
            # XXX nice, in ospray the render region is set on the *camera*,
            # while in blender it is a *render* setting, but we pass it as
            # an *image* setting :)

            # Blender: X to the right, Y up, i.e. (0,0) is lower-left, same
            # as ospray. BUT: ospray always fills up the complete framebuffer
            # with the specified image region, so we don't have a direct
            # equivalent of only rendering a sub-region of the full
            # framebuffer as in blender :-/

            min_x = render.border_min_x
            min_y = render.border_min_y
            max_x = render.border_max_x
            max_y = render.border_max_y

            left = int(min_x*self.framebuffer_width)
            right = int(max_x*self.framebuffer_width)
            bottom = int(min_y*self.framebuffer_height)
            top = int(max_y*self.framebuffer_height)

            # Crop region in ospray is set in normalized screen-space coordinates,
            # i.e. bottom-left of pixel (i,j) is (i,j), but top-right is (i+1,j+1)
            image_settings.border[:] = [
                left/self.framebuffer_width, bottom/self.framebuffer_height,
                (right+1)/self.framebuffer_width, (top+1)/self.framebuffer_height
            ]

            self.framebuffer_width = right - left + 1
            self.framebuffer_height = top - bottom + 1

        image_settings.width = self.framebuffer_width
        image_settings.height = self.framebuffer_height

        # Camera

        cam_obj = scene.camera
        cam_xform = cam_obj.matrix_world
        cam_data = cam_obj.data

        camera_settings = CameraSettings()
        camera_settings.object_name = cam_obj.name
        camera_settings.camera_name = cam_data.name

        camera_settings.aspect = aspect
        camera_settings.clip_start = cam_data.clip_start
        # XXX no far clip in ospray :)

        if cam_data.type == 'PERSP':
            camera_settings.type = CameraSettings.PERSPECTIVE

            # Get camera FOV (in degrees)
            hfov = cam_data.angle
            image_plane_width = 2 * tan(hfov/2)
            image_plane_height = image_plane_width / aspect
            vfov = 2*atan(image_plane_height/2)
            camera_settings.fov_y = degrees(vfov)

        elif cam_data.type == 'ORTHO':
            camera_settings.type = CameraSettings.ORTHOGRAPHIC
            camera_settings.height = cam_data.ortho_scale / aspect

        elif cam_data.type == 'PANO':
            camera_settings.type = CameraSettings.PANORAMIC

        else:
            raise ValueError('Unknown camera type "%s"' % cam_data.type)

        camera_settings.position[:] = list(cam_obj.location)
        camera_settings.view_dir[:] = list(cam_xform @ Vector((0, 0, -1)) - cam_obj.location)
        camera_settings.up_dir[:] = list(cam_xform @ Vector((0, 1, 0)) - cam_obj.location)

        # Depth of field
        camera_settings.dof_focus_distance = 0
        camera_settings.dof_aperture = 0.0

        if cam_data.dof.use_dof:
            dof_settings = cam_data.dof
            if dof_settings.focus_object is not None:
                focus_world = dof_settings.focus_object.matrix_world.translation
                cam_world = cam_obj.matrix_world.translation
                camera_settings.dof_focus_distance = (focus_world - cam_world).length
            else:
                camera_settings.dof_focus_distance = dof_settings.focus_distance

            # Camera focal length in mm + f-stop -> aperture in m
            camera_settings.dof_aperture = (0.5 * cam_data.lens / dof_settings.aperture_fstop) / 1000

        # Render settings

        render_settings = RenderSettings()
        render_settings.renderer = scene.ospray.renderer
        render_settings.background_color[:] = world.ospray.background_color
        self.render_samples = render_settings.samples = scene.ospray.samples
        render_settings.ao_samples = scene.ospray.ao_samples
        #render_settings.shadows_enabled = scene.ospray.shadows_enabled     # XXX removed in 2.0?

        # Lights

        self.engine.update_stats('', 'Exporting lights')

        light_settings = LightSettings()

        light_settings.ambient_color[:] = world.ospray.ambient_color
        light_settings.ambient_intensity = world.ospray.ambient_intensity

        type2enum = dict(POINT=Light.POINT, SUN=Light.SUN, SPOT=Light.SPOT, AREA=Light.AREA)

        lights = []
        for instance in depsgraph.object_instances:

            obj = instance.object

            if obj.type != 'LIGHT':
                continue

            data = obj.data
            xform = obj.matrix_world

            ospray_data = data.ospray

            light = Light()
            light.type = type2enum[data.type]
            light.object2world[:] = matrix2list(xform)
            light.object_name = obj.name
            light.light_name = data.name

            light.color[:] = data.color
            light.intensity = ospray_data.intensity
            light.visible = ospray_data.visible      

            if data.type == 'SUN':
                light.angular_diameter = ospray_data.angular_diameter
            elif data.type != 'AREA':
                light.position[:] = (xform[0][3], xform[1][3], xform[2][3])

            if data.type in ['SUN', 'SPOT']:
                light.direction[:] = obj.matrix_world @ Vector((0, 0, -1)) - obj.location

            if data.type == 'SPOT':
                # Blender:
                # .spot_size = full angle where light shines, in degrees
                # .spot_blend = factor in [0,1], 0 = no penumbra, 1 = penumbra is full angle
                light.opening_angle = degrees(data.spot_size)
                light.penumbra_angle = 0.5*data.spot_blend*degrees(data.spot_size)
                # assert light.penumbra_angle < 0.5*light.opening_angle

            if data.type in ['POINT', 'SPOT']:
                light.radius = data.shadow_soft_size        # XXX what is this called in ospray?

            if data.type == 'AREA':
                size_x = data.size
                size_y = data.size_y

                # Local
                position = Vector((-0.5*size_x, -0.5*size_y, 0))
                edge1 = position + Vector((0, size_y, 0))
                edge2 = position + Vector((size_x, 0, 0))

                # World
                position = obj.matrix_world @ position
                edge1 = obj.matrix_world @ edge1 - position
                edge2 = obj.matrix_world @ edge2 - position

                light.position[:] = position
                light.edge1[:] = edge1
                light.edge2[:] = edge2

            lights.append(light)

        # Assigning to lights[:] doesn't work, need to use extend()
        light_settings.lights.extend(lights)

        #
        # Send scene
        # XXX why isn't the lights export code below?
        #

        # Image settings
        send_protobuf(self.sock, image_settings)

        # Render settings
        send_protobuf(self.sock, render_settings)

        # Camera settings
        send_protobuf(self.sock, camera_settings)

        # Light settings
        send_protobuf(self.sock, light_settings)
        
        # Signal last data was sent!

        element = SceneElement()
        element.type = SceneElement.NONE
        send_protobuf(self.sock, element)        

        # Mesh objects

        for instance in depsgraph.object_instances:

            obj = instance.object

            if obj.type != 'MESH':
                continue                        
                
            mesh = obj.data
            
            # XXX Check if we already sent this mesh
                
            self.send_updated_mesh_data(data, depsgraph, mesh)
            
            # Remember that we exported this mesh
            self.mesh_data_exported.add(mesh.name)
            
            # Send object linking to the mesh data
            self.send_updated_object(data, depsgraph, obj, mesh, instance.matrix_world)
            


    def send_updated_object(self, data, depsgraph, obj, mesh, matrix_world):

        # We do a bit of the logic here in determining what a certain
        # object -> object data combination (including their properties)
        # mean, so the server isn't bothered with this.
    
        client_message = ClientMessage()
        client_message.type = ClientMessage.UPDATE_OBJECT    
        
        update = UpdateObject()
        update.name = obj.name
        update.object2world[:] = matrix2list(matrix_world)
        update.data_link = mesh.name
        
        custom_properties, plugin_parameters = self._process_properties2(obj, False)
        assert len(plugin_parameters.keys()) == 0
        
        update.custom_properties = json.dumps(custom_properties)
        
        if obj.ospray.ospray_override:
            
            if not mesh.ospray.plugin_enabled:
                # No plugin enabled on the linked mesh data, treat
                # as regular blender Mesh object
                update.type = UpdateObject.MESH
                
            else:                
                plugin_type = mesh.ospray.plugin_type
                
                if plugin_type == 'geometry':
                    update.type = UpdateObject.GEOMETRY
                    
                elif plugin_type == 'scene':
                    update.type = UpdateObject.SCENE   
                    
                elif plugin_type == 'volume':
                    
                    # XXX properties: sample rate, single shade, gradient shading, ...
                    
                    volume_usage = obj.ospray.volume_usage
                    if volume_usage == 'volume':
                        update.type = UpdateObject.VOLUME
                    elif volume_usage == 'slices':
                        update.type = UpdateObject.SLICES
                        # XXX process child objects for slices
                    elif volume_usage == 'isosurfaces':
                        # Isosurface values are read from the custom property 'isovalue'
                        update.type = UpdateObject.ISOSURFACES
                        
        else:
            # Regular blender Mesh object
            update.type = UpdateObject.MESH
    
        send_protobuf(self.sock, client_message)
        send_protobuf(self.sock, update)    
    

    def send_updated_mesh_data(self, data, depsgraph, mesh):
        
        """
        Send an update on a Mesh Data block
        """
        
        # XXX check if mesh data was already updated, if so skip
        
        if mesh.ospray.plugin_enabled:
            # Plugin instance
            
            print('Updating plugin-enabled mesh "%s"' % mesh.name)
            
            ospray = mesh.ospray            
            plugin_enabled = ospray.plugin_enabled
            plugin_name = ospray.plugin_name
            plugin_type = ospray.plugin_type
            
            custom_properties, plugin_parameters = self._process_properties2(mesh, True)
            
            self.update_plugin_instance(mesh.name, plugin_type, plugin_name, plugin_parameters, custom_properties)

        else:
            # Treat as regular blender mesh
            # XXX any need to send custom properties?
            self.update_blender_mesh(data, depsgraph, mesh) 


    def update_plugin_instance(self, name, plugin_type, plugin_name, plugin_parameters, custom_properties):
        
        self.engine.update_stats('', 'Updating plugin instance %s (plugin type %s)' % (name, plugin_type))
        
        type2enum = dict(
            geometry = UpdatePluginInstance.GEOMETRY,
            volume = UpdatePluginInstance.VOLUME,
            scene = UpdatePluginInstance.SCENE
        )
        
        client_message = ClientMessage()
        client_message.type = ClientMessage.UPDATE_PLUGIN_INSTANCE            
        
        update = UpdatePluginInstance()
        update.type = type2enum[plugin_type]
        update.name = name
        
        update.plugin_name = plugin_name
        update.plugin_parameters = json.dumps(plugin_parameters)
        update.custom_properties = json.dumps(custom_properties)
        
        send_protobuf(self.sock, client_message)
        send_protobuf(self.sock, update)
        
        generate_function_result = GenerateFunctionResult()
        
        receive_protobuf(self.sock, generate_function_result)
        
        if not generate_function_result.success:
            print('ERROR: plugin generation failed:')
            print(generate_function_result.message)
            return
        

    def update_blender_mesh(self, data, depsgraph, mesh):
    
        # XXX we should export meshes separately, keeping a local
        # list which ones we already exported (by name).
        # Then for MESH objects use the name of the mesh to instantiate
        # it using the given xform. This gives us real instancing.
        # But a user can change a mesh's name. However, we can
        # sort of handle this by using the local name list and deleting
        # (also on the server) whichever name's we don't see when exporting.
        # Could also set a custom property on meshes with a unique ID
        # we choose ourselves. But props get copied when duplicating
        # See https://devtalk.blender.org/t/universal-unique-id-per-object/363/3

        self.engine.update_stats('', 'Updating Blender mesh %s' % mesh.name)
        
        client_message = ClientMessage()
        client_message.type = ClientMessage.UPDATE_BLENDER_MESH       
        client_message.string_value = mesh.name
        
        send_protobuf(self.sock, client_message)
        
        # Send the actual mesh geometry
        
        mesh_data = MeshData()
        flags = 0

        # Send (triangulated) geometry

        mesh.calc_loop_triangles()

        nv = mesh_data.num_vertices = len(mesh.vertices)
        nt = mesh_data.num_triangles = len(mesh.loop_triangles)

        print('Mesh %s: %d v, %d t' % (mesh.name, nv, nt))

        # Check if any faces use smooth shading
        # XXX we currently don't handle meshes with both smooth
        # and non-smooth faces, but those are probably not very common anyway

        use_smooth = False
        for tri in mesh.loop_triangles:
            if tri.use_smooth:
                print('Mesh uses smooth shading')
                use_smooth = True
                flags |= MeshData.NORMALS
                break

        # Vertex colors
        #https://blender.stackexchange.com/a/8561
        if mesh.vertex_colors:
            flags |= MeshData.VERTEX_COLORS

        # Send mesh data

        mesh_data.flags = flags

        send_protobuf(self.sock, mesh_data)

        # Send vertices

        vertices = numpy.empty(nv*3, dtype=numpy.float32)

        for idx, v in enumerate(mesh.vertices):
            p = v.co
            vertices[3*idx+0] = p.x
            vertices[3*idx+1] = p.y
            vertices[3*idx+2] = p.z
            
        #print(vertices)

        self.sock.send(vertices.tobytes())

        # Send vertex normals (if set)

        if use_smooth:
            normals = numpy.empty(nv*3, dtype=numpy.float32)

            for idx, v in enumerate(mesh.vertices):
                # XXX use .index?
                n = v.normal
                normals[3*idx+0] = n.x
                normals[3*idx+1] = n.y
                normals[3*idx+2] = n.z

            self.sock.send(normals.tobytes())

        # Send vertex colors (if set)

        if mesh.vertex_colors:
            vcol_layer = mesh.vertex_colors.active
            vcol_data = vcol_layer.data

            vertex_colors = numpy.empty(nv*4, dtype=numpy.float32)

            for poly in mesh.polygons:
                for loop_index in poly.loop_indices:
                    loop_vert_index = mesh.loops[loop_index].vertex_index
                    color = vcol_data[loop_index].color
                    # RGBA vertex colors in Blender 2.8x
                    vertex_colors[4*loop_vert_index+0] = color[0]
                    vertex_colors[4*loop_vert_index+1] = color[1]
                    vertex_colors[4*loop_vert_index+2] = color[2]
                    vertex_colors[4*loop_vert_index+3] = 1.0

            self.sock.send(vertex_colors.tobytes())

        # Send triangles

        triangles = numpy.empty(nt*3, dtype=numpy.uint32)   # XXX opt possible with <64k vertices ;-)

        for idx, tri in enumerate(mesh.loop_triangles):
            triangles[3*idx+0] = tri.vertices[0]
            triangles[3*idx+1] = tri.vertices[1]
            triangles[3*idx+2] = tri.vertices[2]
            
        #print(triangles)

        self.sock.send(triangles.tobytes())


                
                
    #
    # Rendering
    #
    
    def render(self, depsgraph):

        """
        if self.is_preview:
            self.render_preview(depsgraph)
        else:
            self.render_scene(depsgraph)
        """

        # Signal server to start rendering

        client_message = ClientMessage()
        client_message.type = ClientMessage.START_RENDERING
        send_protobuf(self.sock, client_message)

        # Read back successive framebuffer samples

        num_pixels = self.framebuffer_width * self.framebuffer_height
        bytes_left = num_pixels*4 * 4

        framebuffer = numpy.zeros(bytes_left, dtype=numpy.uint8)

        t0 = time.time()

        result = self.engine.begin_result(0, 0, self.framebuffer_width, self.framebuffer_height)
        # Only Combined and Depth seem to be available
        layer = result.layers[0].passes["Combined"]

        FBFILE = '/dev/shm/blosprayfb.exr'

        sample = 1
        cancel_sent = False

        self.engine.update_stats('', 'Rendering sample %d/%d' % (sample, self.render_samples))

        # XXX this loop blocks too often, might need to move it to a separate thread,
        # but OTOH we're already using select() to detect when to read
        while True:

            # Check for incoming render results

            r, w, e = select.select([self.sock], [], [], 0)

            if len(r) == 1:

                render_result = RenderResult()
                # XXX handle receive error
                receive_protobuf(self.sock, render_result)

                if render_result.type == RenderResult.FRAME:

                    # New framebuffer (for a single pixel sample) is available

                    """
                    # XXX Slow: get as raw block of floats
                    print('[%6.3f] _read_framebuffer start' % (time.time()-t0))
                    self._read_framebuffer(framebuffer, self.framebuffer_width, self.framebuffer_height)
                    print('[%6.3f] _read_framebuffer end' % (time.time()-t0))

                    pixels = framebuffer.view(numpy.float32).reshape((num_pixels, 4))
                    print(pixels.shape)
                    print('[%6.3f] view() end' % (time.time()-t0))

                    # Here we write the pixel values to the RenderResult
                    # XXX This is the slow part
                    print(type(layer.rect))
                    layer.rect = pixels
                    self.update_result(result)
                    """

                    # We read the framebuffer file content from the server
                    # and locally write it to FBFILE, which then gets loaded by Blender

                    # XXX both receiving into a file and loading from file 
                    # block the blender UI for a short time

                    print('[%6.3f] _read_framebuffer_to_file start' % (time.time()-t0))
                    self._read_framebuffer_to_file(FBFILE, render_result.file_size)
                    print('[%6.3f] _read_framebuffer_to_file end' % (time.time()-t0))

                    # Sigh, this needs an image file format. I.e. reading in a raw framebuffer
                    # of floats isn't possible, hence the OpenEXR file
                    # XXX result.load_from_file(...), instead of result.layers[0].load_from_file(...), would work as well?
                    result.layers[0].load_from_file(FBFILE)

                    # Remove file
                    os.unlink(FBFILE)

                    self.engine.update_result(result)

                    print('[%6.3f] update_result() done' % (time.time()-t0))

                    self.engine.update_progress(sample/self.render_samples)

                    sample += 1

                    # XXX perhaps use update_memory_stats()

                    self.engine.update_stats(
                        'Server %.1f MB' % render_result.memory_usage,
                        'Rendering sample %d/%d' % (sample, self.render_samples))

                elif render_result.type == RenderResult.CANCELED:
                    print('Rendering CANCELED!')
                    self.engine.update_stats('', 'Rendering canceled')
                    cancel_sent = True
                    break

                elif render_result.type == RenderResult.DONE:
                    self.engine.update_stats('', 'Rendering done')
                    print('Rendering done!')
                    break

            # Check if render was canceled

            if self.engine.test_break() and not cancel_sent:
                client_message = ClientMessage()
                client_message.type = ClientMessage.CANCEL_RENDERING
                send_protobuf(self.sock, client_message)
                cancel_sent = True

            time.sleep(0.001)

        self.engine.end_result(result)

        print('Done with render loop')

        client_message = ClientMessage()
        client_message.type = ClientMessage.QUIT
        send_protobuf(self.sock, client_message)    

    # Utility

    """
    def _read_framebuffer(self, framebuffer, width, height):

        # XXX use select() in a loop, to allow UI updates more frequently

        num_pixels = width * height
        bytes_left = num_pixels * 4*4

        #self.update_stats('%d bytes left' % bytes_left, 'Reading back framebuffer')

        view = memoryview(framebuffer)

        while bytes_left > 0:
            n = self.sock.recv_into(view, bytes_left)
            view = view[n:]
            bytes_left -= n
            sys.stdout.write('.')
            sys.stdout.flush()
            #self.update_stats('%d bytes left' % bytes_left, 'Reading back framebuffer')
    """
    
    def _read_framebuffer_to_file(self, fname, size):

        print('_read_framebuffer_to_file(%s, %d)' % (fname, size))

        # XXX use select() in a loop, to allow UI updates more frequently

        with open(fname, 'wb') as f:
            bytes_left = size
            while bytes_left > 0:
                d = self.sock.recv(bytes_left)
                # XXX check d
                f.write(d)
                bytes_left -= len(d)
