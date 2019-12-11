// ======================================================================== //
// BLOSPRAY - OSPRay as a Blender render engine                             //
// Paul Melis, SURFsara <paul.melis@surfsara.nl>                            //
// Utility routines                                                         //
// ======================================================================== //
// Copyright 2018-2019 SURFsara                                             //
//                                                                          //
// Licensed under the Apache License, Version 2.0 (the "License");          //
// you may not use this file except in compliance with the License.         //
// You may obtain a copy of the License at                                  //
//                                                                          //
//     http://www.apache.org/licenses/LICENSE-2.0                           //
//                                                                          //
// Unless required by applicable law or agreed to in writing, software      //
// distributed under the License is distributed on an "AS IS" BASIS,        //
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. //
// See the License for the specific language governing permissions and      //
// limitations under the License.                                           //
// ======================================================================== //

#ifndef UTIL_INTERNAL_H
#define UTIL_INTERNAL_H

//#define DUMP_PROTOBUF_TRAFFIC

#include <cstdlib>
#include <iostream>
#include <fstream>
#include <boost/version.hpp>
#if BOOST_VERSION >= 106600
#include <boost/uuid/detail/sha1.hpp>
#else
#include <boost/uuid/sha1.hpp>
#endif
#include <boost/regex.hpp>
#include <glm/gtc/type_ptr.hpp>

// Protobuf message printing
#include <google/protobuf/io/zero_copy_stream_impl.h>
#include <google/protobuf/text_format.h>

#include <ospray/ospray.h>
#include "tcpsocket.h"

/*
void
affine3f_from_rowmajor(osp::affine3f& xform, float *m)
{
    xform.l.vx = osp::vec3f{ m[0], m[4], m[8] };
    xform.l.vy = osp::vec3f{ m[1], m[5], m[9] };
    xform.l.vz = osp::vec3f{ m[2], m[6], m[10] };
    xform.p    = osp::vec3f{ m[3], m[7], m[11] };
}

void
affine3f_from_colmajor(osp::affine3f& xform, float *m)
{
    xform.l.vx = osp::vec3f{ m[0], m[1], m[2] };
    xform.l.vy = osp::vec3f{ m[4], m[5], m[6] };
    xform.l.vz = osp::vec3f{ m[8], m[9], m[10] };
    xform.p    = osp::vec3f{ m[12], m[13], m[14] };
}

void
affine3f_from_mat4(osp::affine3f& xform, const glm::mat4 &mat)
{
    const float *M = glm::value_ptr(mat);

    xform.l.vx.x = M[0];
    xform.l.vx.y = M[1];
    xform.l.vx.z = M[2];

    xform.l.vy.x = M[4];
    xform.l.vy.y = M[5];
    xform.l.vy.z = M[6];

    xform.l.vz.x = M[8];
    xform.l.vz.y = M[9];
    xform.l.vz.z = M[10];

    xform.p.x = M[12];
    xform.p.y = M[13];
    xform.p.z = M[14];
}

osp::affine3f
affine3f_from_mat4(const glm::mat4 &mat)
{
    osp::affine3f xform;
    affine3f_from_mat4(xform, mat);
    return xform;
}
*/

void
affine3fv_from_mat4(float *xform, const glm::mat4 &mat)
{
    const float *M = glm::value_ptr(mat);

    xform[0] = M[0];
    xform[1] = M[1];
    xform[2] = M[2];

    xform[3] = M[4];
    xform[4] = M[5];
    xform[5] = M[6];

    xform[6] = M[8];
    xform[7] = M[9];
    xform[8] = M[10];

    xform[9] = M[12];
    xform[10] = M[13];
    xform[11] = M[14];
}

static uint8_t receive_buffer[1024];

template<typename T>
bool
receive_protobuf(TCPSocket *sock, T& protobuf)
{    
    uint32_t message_size;
    
    if (sock->recvall(&message_size, 4) == -1)
        return false;

    assert(message_size < 1024);
        
    if (sock->recvall(receive_buffer, message_size) == -1)
        return false;

    protobuf.ParseFromArray(receive_buffer, message_size);

#ifdef DUMP_PROTOBUF_TRAFFIC    
    fprintf(stderr, "--- receive_protobuf() ---\n%s\n--------------------------\n", protobuf.DebugString().c_str());
#endif

    return true;
}

template<typename T>
bool
send_protobuf(TCPSocket *sock, T& protobuf)
{
    std::string message;
    uint32_t message_size;

#ifdef DUMP_PROTOBUF_TRAFFIC
    fprintf(stderr, "--- send_protobuf() ---\n%s\n-----------------------\n", protobuf.DebugString().c_str());
#endif
    
    // XXX use SerializeToArray
    protobuf.SerializeToString(&message);
    
    message_size = message.size();
    
    if (sock->send((uint8_t*)&message_size, 4) == -1)
        return false;
    
    if (sock->sendall((uint8_t*)&message[0], message_size) == -1)
        return false;
    
    return true;
}

template<typename T>
void
object2world_from_protobuf(glm::mat4 &matrix, T& protobuf)
{
    float *M = glm::value_ptr(matrix);
    
    // Protobuf elements assumed in row-major order 
    // (while GLM uses column-major order)
    M[0] = protobuf.object2world(0);
    M[1] = protobuf.object2world(4);
    M[2] = protobuf.object2world(8);
    M[3] = protobuf.object2world(12);

    M[4] = protobuf.object2world(1);
    M[5] = protobuf.object2world(5);
    M[6] = protobuf.object2world(9);
    M[7] = protobuf.object2world(13);
    
    M[8] = protobuf.object2world(2);
    M[9] = protobuf.object2world(6);
    M[10] = protobuf.object2world(10);
    M[11] = protobuf.object2world(14);
    
    M[12] = protobuf.object2world(3);
    M[13] = protobuf.object2world(7);
    M[14] = protobuf.object2world(11);
    M[15] = protobuf.object2world(15);    
}

template<typename T>
void
print_protobuf(const T& protobuf)
{
    google::protobuf::io::FileOutputStream* output = new google::protobuf::io::FileOutputStream(1);         // stdout
    
    // XXX add message type
    printf("--- %s message ---\n", protobuf.GetTypeName().c_str());
    bool success = google::protobuf::TextFormat::Print(protobuf, output);
    output->Flush();
    printf("------------------------\n");
        
    delete output;
}

// https://stackoverflow.com/a/39833022/9296788
std::string 
get_sha1(const std::string& p_arg)
{
    boost::uuids::detail::sha1 sha1;
    sha1.process_bytes(p_arg.data(), p_arg.size());
    unsigned hash[5] = {0};
    sha1.get_digest(hash);

    // Back to string
    char buf[41] = {0};

    for (int i = 0; i < 5; i++)
    {
        std::sprintf(buf + (i << 3), "%08x", hash[i]);
    }

    return std::string(buf);
}

// Replace "... $<envvar> ..."
std::string 
process_environment_variables_match(const boost::smatch& what)
{
    std::string full(what[0].first, what[0].second);
    std::string env(what[1].first, what[1].second);
    
    const char *value = std::getenv(env.c_str());
    
    if (value == nullptr)
    {
        // Return unmodified full environment string,
        // including the $<...> markers
        printf("... WARNING: environment variable '%s' not set in '%s'\n", 
            env.c_str(), full.c_str());
        return full;
    }
    
    return std::string(value);
}

std::string
replace_environment_variables(const std::string& s)
{
    boost::regex pat("\\$<([^>]+?)>");
    
    return boost::regex_replace(s,
        pat, 
        process_environment_variables_match, 
        boost::match_default | boost::format_all);
}

// Return memory usage in megabytes
// Linux only atm
float
memory_usage()
{
#ifdef linux
    // Based on https://stackoverflow.com/a/12675172/9296788
    /*
    Provides information about memory usage, measured in pages.  The columns are:
    size     (1) total program size
             (same as VmSize in /proc/[pid]/status)
    resident (2) resident set size
             (same as VmRSS in /proc/[pid]/status)
    shared   (3) number of resident shared pages (i.e., backed by a file)
             (same as RssFile+RssShmem in /proc/[pid]/status)
    text     (4) text (code)
    lib      (5) library (unused since Linux 2.6; always 0)
    data     (6) data + stack
    dt       (7) dirty pages (unused since Linux 2.6; always 0)
    */    
    uint64_t total = 0, resident = 0, share = 0;
    std::ifstream buffer("/proc/self/statm", std::ifstream::in);
    buffer >> total >> resident >> share;
    buffer.close();

    float page_size_mb = 1.0f * sysconf(_SC_PAGE_SIZE) / (1000*1000); 
    float rss = resident * page_size_mb;
    
    return rss;
#else
    return 0;    
#endif
}

#endif
