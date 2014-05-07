
from pox.core import core
from pox.lib.addresses import IPAddr, IPAddr6, EthAddr
from pox.lib.util import dpid_to_str, str_to_dpid
from pox.openflow.discovery import *
from pox.openflow.of_json import *
from pox.web.jsonrpc import JSONRPCHandler, make_error
from heapq import *
from threading import Thread
import pox.lib.packet as pkt
import pox.openflow.libopenflow_01 as of
import inspect
import os
import socket
import struct
import sys

log = core.getLogger()

def is_host(name):
	return name.find(".") > 0

def is_datapath(name):
	return name.find(":") > 0

dpid_name_dict = {}
dpid_name_dict["96-d0-db-91-0a-44"] = "switch-01"
dpid_name_dict["3e-25-98-57-0a-4e"] = "switch-02"
dpid_name_dict["4e-5d-91-a4-26-4d"] = "switch-03"
dpid_name_dict["e2-94-27-d5-ef-4e"] = "switch-04"
dpid_name_dict["2e-7a-18-38-8c-49"] = "switch-05"
dpid_name_dict["66-5d-a4-6c-ac-41"] = "switch-06"
dpid_name_dict["2a-db-19-bc-94-4a"] = "host-01"
dpid_name_dict["7e-1f-d6-e4-84-4e"] = "host-02"
dpid_name_dict["ee-14-c4-6a-d3-4f"] = "host-03"
dpid_name_dict["8e-23-ea-7a-73-48"] = "host-04"
dpid_name_dict["52-84-05-47-56-4e"] = "host-05"
dpid_name_dict["8a-68-d2-8b-e6-41"] = "host-06"
dpid_name_dict["ce-b8-5c-71-5e-4f"] = "host-07"
dpid_name_dict["4a-84-54-fd-db-43"] = "host-08"
dpid_name_dict["6a-59-d5-d4-92-44"] = "host-09"
dpid_name_dict["fe-92-3d-be-8c-47"] = "host-10"
dpid_name_dict["1a-ab-10-e1-c8-47"] = "host-11"
dpid_name_dict["fe-98-29-28-fa-4a"] = "host-12"
dpid_name_dict["9a-29-05-08-c0-47"] = "host-13"
dpid_name_dict["9a-f2-e1-da-9e-46"] = "host-14"
dpid_name_dict["8e-6c-82-fe-89-48"] = "host-15"
dpid_name_dict["6a-35-ac-ba-48-46"] = "host-16"
def dpid_to_switch_name(name):
	return dpid_name_dict[name]

class MyComponent(object):

	def __init__(self):
		core.listen_to_dependencies(self, ['openflow_discovery'])
		core.openflow.addListeners(self)
		
		#self._switch_list = []
		self._proc_num = None
		self._host_list = {}
		self._datapath_list = {}

		self._topo_graph = Graph()
		self._conn_graph = Graph()
		self._reduce_plan = {}

		self._host_host_path = {}  # shortest path from host to host

		self._mac_to_port = {}

		self._link_num = 0

		thread = Thread(target = self._handle_ConnectionFromHost)
		thread.start()

	def __enter__(self):
		return self

	def __exit__(self, type, value, traceback):
		thread.join()
		print "Thread Finish"
		for h in self._host_list:
			self._host_list[h].sock.close()
			
	def _host_name_to_rank(self, name):
		return self._host_list[name].rank

	def _handle_ConnectionFromHost(self):
		while (True): # loop for application
			s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
			s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
			s.bind(('',65432))
			s.listen(64)

			def accept_host_connection():
				c,addr = s.accept()
				public_addr = addr[0]
				host_info = c.recv(100)
				#print "Got connection from", public_addr, " and receive : ", host_info
				[private_addr, mac_address, host_rank, proc_num] = host_info.split(" ")

				# wait for host arp
				while private_addr not in self._host_list:
					time.sleep(0.01)

				host_obj = self._host_list[private_addr]
				host_obj.public_ip = public_addr
				host_obj.mac_address = mac_address
				host_obj.rank = host_rank
				host_obj.sock = c

				self._proc_num = int(proc_num)

			# accept first host to get processes number
			accept_host_connection()

			# accept the rest host
			for i in range(self._proc_num - 1):
				accept_host_connection()

			# close TODO: reset for reuse in next MPI application
			s.close()

			# after get all host data, calculate reduction plan and send back
			print "Creating connection graph"
			self._construct_connected_graph()  # result is in self._conn_graph

			# calculate root value
			for dpid in self._datapath_list:
				root_value = self._topo_graph.get_root_value(dpid_to_str(dpid))
				self._datapath_list[dpid].root_value = root_value

			# create binomial tree and plan
			print "Creating binomial tree for reduce to each rank"
			self._construct_binomial_tree_and_plan(dump = False)

			# TODO: close socket connection
			for host in self._host_list:
				# string buffer to send
				buf = str(len(self._host_list)) + " " # append root num
				for root in self._host_list:
					buf += str(self._host_list[root].rank) + " "

					step_count = 0
					step_buffer = ""
					for send_recv in self._reduce_plan[root]:
						(level,src,dst) = send_recv
						if host == src or host == dst:
							step_count += 1
							step_buffer += str(self._host_list[src].rank) + " "
							step_buffer += str(self._host_list[dst].rank) + " "

					buf += str(step_count) + " "
					buf += step_buffer

				self._host_list[host].sock.send(buf)

			# create minimum host spanning tree in install flow for bcast
			mhst = self._topo_graph.minimum_host_spanning_tree(dump = False)
			self._install_flow_bcast(mhst)

			# send mac address of each process
			buf = ""
			for h in self._host_list:
				buf += str(self._host_list[h].rank) + " " + str(self._host_list[h].mac_address) + " " + h + " "
			for h in self._host_list:
				self._host_list[h].sock.send(buf)

			# reduce
			#self._construct_allreduce_plan()
			#self._install_flow_reduce_path(reduce_level, reduce_root)

	def _handle_ConnectionUp(self, event):
		#self._switch_list.append(dpid_to_str(event.connection.dpid))
		#log.debug("Switch in list : " + str(self._switch_list))
		
		"""
		log.debug("Connect from : " + dpid_to_str(event.connection.dpid))

		log.debug("Original ports : " + str(event.connection.original_ports))
		ports = event.connection.ports
		for port in ports:
			log.debug("Port : " + str(port) + " : " + str(ports[port]))
		#log.debug("Port : " + str(event.connection.ports))
		"""

		"""  event.connection
		'addListener', 'addListenerByName', 'addListeners', 'buf', 'clearHandlers', 'close', 
		'connect_time', 'disconnect', 'disconnected', 'disconnection_raised', 'dpid', 'err', 
		'eth_addr', 'features', 'fileno', 'idle_time', 'info', 'listenTo', 'msg', 'ofnexus', 
		'original_ports', 'ports', 'raiseEvent', 'raiseEventNoErrors', 'read', 'removeListener', 
		'removeListeners', 'send', 'sock'
		"""

		dpid = event.connection.dpid

		if dpid not in self._datapath_list:
			new_datapath = NetworkDatapath()
			new_datapath.dpid = dpid
			self._datapath_list[dpid] = new_datapath

		self._datapath_list[dpid].connection = event.connection

		self._install_flow_detect_host_topology(event.connection)
		self._install_loop_terminate_flow(event.connection)

	#def _handle_ConnectionDown(self, event):

	def _handle_PortStatus(self, event):
		if event.added: # add port
			action = "added"
		elif event.deleted: # delete port
			action = "removed"
		else:
			action = "modified"
			print str(dir(event.modified))
			#.port
		log.debug("Port " + str(event.port) + " on switch " + dpid_to_str(event.connection.dpid) + " has been " + action)

	"""
	def _handle_FlowRemoved(self, event):
		# get removed reason
	"""

	def _handle_PacketIn(self, event):
		dpid = event.connection.dpid
		packet_data = event.parsed
		in_port = event.port

		# ignore incomplete packet
		if not packet_data.parsed:
			log.warning("Ignoring incomplete packet")
			return

		packet_in = event.ofp   # ofp_packet_in

		#if packet_data.type == pkt.ethernet.LLDP_TYPE:
			#lldph = packet.find(pkt.lldp)
			#if lldph is None or not lldph.parsed:
			#	return

		# if packet is asp request from host
		arp_packet = packet_data.find("arp")
		if arp_packet and str(arp_packet.protosrc) == str(arp_packet.protodst):
			#print "arp_packet.protosrc = " + str(arp_packet.protosrc)
			ip_src = str(arp_packet.protosrc)
			if ip_src in self._host_list:
				return

			self._add_link_to_graph(dpid, ip_src, in_port, 1)
			new_host = NetworkHost()
			new_host.private_ip = ip_src
			new_host.adjacent_datapath = dpid_to_str(dpid)
			self._host_list[ip_src] = new_host

			log.info("link detected: " + ip_src + " -> " + dpid_to_str(dpid))

			# create shortest path and install flow for other packet
			self._host_host_path[ip_src] = {}
			for host in self._host_host_path:
				if host != ip_src:
					self._host_host_path[ip_src][host] = self._topo_graph.k_shortest_paths(ip_src, host, 1)[0]
					self._install_flow_shortest_path(ip_src, host)
					if ip_src not in self._host_host_path[host]:
						self._host_host_path[host][ip_src] = self._topo_graph.k_shortest_paths(host, ip_src, 1)[0]
						self._install_flow_shortest_path(host, ip_src)

			return

		# if packet is ip protocol
		if packet_data.type == pkt.ethernet.IP_TYPE:
			ip_packet = packet_data.payload
			
		"""
			# check if TCP
			if ip_packet.protocol == pkt.ipv4.TCP_PROTOCOL:
				tcp_packet = ip_packet.payload
				s = str(ip_packet.srcip)+":"+str(tcp_packet.srcport)+"->"+str(ip_packet.dstip)+":"+str(tcp_packet.dstport)
				if s not in self._tmp_dict:
					self._tmp_dict[s] = 1
					print "TCP: " + s
				#print vars(ip_packet)
				#print "dstip : " + str(ip_packet.dstip)
				#print "srcip : " + str(ip_packet.srcip)
				
			elif ip_packet.protocol == pkt.ipv4.UDP_PROTOCOL:
				print "UDP Packet gotten"
				udp_packet = ip_packet.payload
				s = str(ip_packet.srcip)+":"+str(udp_packet.srcport)+"->"+str(ip_packet.dstip)+":"+str(udp_packet.dstport)
				if s not in self._tmp_dict2:
					self._tmp_dict2[s] = 1
					print "UDP: " + s
			elif ip_packet.protocol == pkt.ipv4.ICMP_PROTOCOL:
				print "ICMP Packet gotten"
				
			elif ip_packet.protocal == pkt.ipv4.IGMP_PROTOCAL:
				print "IGMP Packet gotten"
		# non ip packet	
		elif packet_data.type != 2054:
			print "NON IP PACKET : " + str(vars(packet_data))
		"""

		# TODO: change to L3
		dl_src = packet_data.src
		dl_dst = packet_data.dst
		# create mac to port map for each datapath
		if dpid not in self._mac_to_port:
			self._mac_to_port[dpid] = {}
		self._mac_to_port[dpid][dl_src] = in_port
		print "switch ",dpid_to_str(dpid),"is learn address",dl_src,"from port",in_port
		if dl_dst in self._mac_to_port[dpid]:
			out_port = self._mac_to_port[dpid][dl_dst]
			packet_out = of.ofp_packet_out()
			packet_out.data = packet_in
			packet_out.actions.append( of.ofp_action_output(port = out_port) )
			event.connection.send(packet_out)

			# add flow mod
			match = of.ofp_match()
			#match.dl_type = pkt.ethernet.IP_TYPE
			#match.in_port = in_port
			match.dl_dst = EthAddr(dl_dst)
			msg = of.ofp_flow_mod()
			msg.priority = 10000
			msg.match = match
			msg.actions.append( of.ofp_action_output(port = out_port) )
			event.connection.send(msg)
		else:
			#print "UNKNOWN PACKET"
			# send other packet to all port
			packet_out = of.ofp_packet_out()
			packet_out.data = packet_in
			packet_out.actions.append( of.ofp_action_output(port = of.OFPP_ALL) )
			event.connection.send(packet_out)

	def _install_loop_terminate_flow(self, connection):

		def install_inout_port(connection, in_port, out_port_list):
			for out_port in out_port_list:
				match = of.ofp_match()
				match.in_port = in_port
				msg = of.ofp_flow_mod()
				msg.priority = 15000
				msg.match = match
				msg.actions.append( of.ofp_action_output(port = out_port) )
				connection.send(msg)

		def install_dst_flow(connection, dst_list, out_port):
			for dst in dst_list:
				match = of.ofp_match()
				match.dl_dst = EthAddr(dst)
				msg = of.ofp_flow_mod()
				msg.priority = 15000
				msg.match = match
				msg.actions.append( of.ofp_action_output(port = out_port) )
				connection.send(msg)

		if connection.dpid == str_to_dpid("96-d0-db-91-0a-44"):
			# 1(gre17), 2(gre18), 3(gre19), 4(gre20)
			install_dst_flow(connection, ["b6:75:f6:00:77:73","da:01:2e:66:f5:b9","fe:40:67:e8:f2:f3","2e:90:aa:80:59:16"], 1)
			install_dst_flow(connection, ["9a:9f:fd:c4:c9:57","32:35:2c:82:ae:3d","0e:54:e6:c0:54:6b","82:33:10:71:04:3c"], 2)
			install_dst_flow(connection, ["0e:80:25:28:1c:82","66:e2:51:99:b3:60","f2:0d:f2:5c:ed:7d","72:72:46:ee:26:64"], 3)
			install_dst_flow(connection, ["7e:21:05:b9:c1:35","72:5a:9d:3f:05:be","3a:73:5b:bd:5d:56","22:f7:af:9c:7b:b5"], 4)
		elif connection.dpid == str_to_dpid("3e-25-98-57-0a-4e"):
			# 1(gre21), 2(gre22), 3(gre23), 4(gre24)
			install_dst_flow(connection, ["b6:75:f6:00:77:73","da:01:2e:66:f5:b9","fe:40:67:e8:f2:f3","2e:90:aa:80:59:16"], 1)
			install_dst_flow(connection, ["9a:9f:fd:c4:c9:57","32:35:2c:82:ae:3d","0e:54:e6:c0:54:6b","82:33:10:71:04:3c"], 2)
			install_dst_flow(connection, ["0e:80:25:28:1c:82","66:e2:51:99:b3:60","f2:0d:f2:5c:ed:7d","72:72:46:ee:26:64"], 3)
			install_dst_flow(connection, ["7e:21:05:b9:c1:35","72:5a:9d:3f:05:be","3a:73:5b:bd:5d:56","22:f7:af:9c:7b:b5"], 4)
		elif connection.dpid == str_to_dpid("4e-5d-91-a4-26-4d"):
			# 1(gre1), 2(gre2), 3(gre3), 4(gre4), 5(gre17), 6(gre21)
			install_dst_flow(connection, ["b6:75:f6:00:77:73"], 1)
			install_dst_flow(connection, ["da:01:2e:66:f5:b9"], 2)
			install_dst_flow(connection, ["fe:40:67:e8:f2:f3"], 3)
			install_dst_flow(connection, ["2e:90:aa:80:59:16"], 4)
			install_dst_flow(connection, ["9a:9f:fd:c4:c9:57","32:35:2c:82:ae:3d","0e:54:e6:c0:54:6b","82:33:10:71:04:3c"], 5)
			install_dst_flow(connection, ["0e:80:25:28:1c:82","66:e2:51:99:b3:60","f2:0d:f2:5c:ed:7d","72:72:46:ee:26:64"], 6)
			install_dst_flow(connection, ["7e:21:05:b9:c1:35","72:5a:9d:3f:05:be","3a:73:5b:bd:5d:56","22:f7:af:9c:7b:b5"], 6)
		elif connection.dpid == str_to_dpid("e2-94-27-d5-ef-4e"):
			# 1(gre5), 2(gre6), 3(gre7), 4(gre8), 5(gre18), 6(gre22)
			install_dst_flow(connection, ["9a:9f:fd:c4:c9:57"], 1)
			install_dst_flow(connection, ["32:35:2c:82:ae:3d"], 2)
			install_dst_flow(connection, ["0e:54:e6:c0:54:6b"], 3)
			install_dst_flow(connection, ["82:33:10:71:04:3c"], 4)
			install_dst_flow(connection, ["b6:75:f6:00:77:73","da:01:2e:66:f5:b9","fe:40:67:e8:f2:f3","2e:90:aa:80:59:16"], 5)
			install_dst_flow(connection, ["0e:80:25:28:1c:82","66:e2:51:99:b3:60","f2:0d:f2:5c:ed:7d","72:72:46:ee:26:64"], 6)
			install_dst_flow(connection, ["7e:21:05:b9:c1:35","72:5a:9d:3f:05:be","3a:73:5b:bd:5d:56","22:f7:af:9c:7b:b5"], 6)
		elif connection.dpid == str_to_dpid("2e-7a-18-38-8c-49"):
			# 1(gre9), 2(gre10), 3(gre11), 4(gre12), 5(gre19), 6(gre23)
			install_dst_flow(connection, ["0e:80:25:28:1c:82"], 1)
			install_dst_flow(connection, ["66:e2:51:99:b3:60"], 2)
			install_dst_flow(connection, ["f2:0d:f2:5c:ed:7d"], 3)
			install_dst_flow(connection, ["72:72:46:ee:26:64"], 4)
			install_dst_flow(connection, ["b6:75:f6:00:77:73","da:01:2e:66:f5:b9","fe:40:67:e8:f2:f3","2e:90:aa:80:59:16"], 5)
			install_dst_flow(connection, ["9a:9f:fd:c4:c9:57","32:35:2c:82:ae:3d","0e:54:e6:c0:54:6b","82:33:10:71:04:3c"], 5)
			install_dst_flow(connection, ["7e:21:05:b9:c1:35","72:5a:9d:3f:05:be","3a:73:5b:bd:5d:56","22:f7:af:9c:7b:b5"], 6)
		elif connection.dpid == str_to_dpid("66-5d-a4-6c-ac-41"):
			# 1(gre13), 2(gre14), 3(gre15), 4(gre16), 5(gre20), 6(gre24)
			install_dst_flow(connection, ["7e:21:05:b9:c1:35"], 1)
			install_dst_flow(connection, ["72:5a:9d:3f:05:be"], 2)
			install_dst_flow(connection, ["3a:73:5b:bd:5d:56"], 3)
			install_dst_flow(connection, ["22:f7:af:9c:7b:b5"], 4)
			install_dst_flow(connection, ["b6:75:f6:00:77:73","da:01:2e:66:f5:b9","fe:40:67:e8:f2:f3","2e:90:aa:80:59:16"], 5)
			install_dst_flow(connection, ["9a:9f:fd:c4:c9:57","32:35:2c:82:ae:3d","0e:54:e6:c0:54:6b","82:33:10:71:04:3c"], 5)
			install_dst_flow(connection, ["0e:80:25:28:1c:82","66:e2:51:99:b3:60","f2:0d:f2:5c:ed:7d","72:72:46:ee:26:64"], 6)
		elif connection.dpid == str_to_dpid("2a-db-19-bc-94-4a"):
			# host-01 switch  1(tap0), 2(gre1)
			install_inout_port(connection, 1, [2])
			install_inout_port(connection, 2, [1])
		elif connection.dpid == str_to_dpid("7e-1f-d6-e4-84-4e"):
			# host-02 switch  1(tap0), 2(gre2)
			install_inout_port(connection, 1, [2])
			install_inout_port(connection, 2, [1])
		elif connection.dpid == str_to_dpid("ee-14-c4-6a-d3-4f"):
			# host-03 switch  1(tap0), 2(gre3)
			install_inout_port(connection, 1, [2])
			install_inout_port(connection, 2, [1])
		elif connection.dpid == str_to_dpid("8e-23-ea-7a-73-48"):
			# host-04 switch  1(tap0), 2(gre4)
			install_inout_port(connection, 1, [2])
			install_inout_port(connection, 2, [1])
		elif connection.dpid == str_to_dpid("52-84-05-47-56-4e"):
			# host-05 switch  1(tap0), 2(gre5)
			install_inout_port(connection, 1, [2])
			install_inout_port(connection, 2, [1])
		elif connection.dpid == str_to_dpid("8a-68-d2-8b-e6-41"):
			# host-06 switch  1(tap0), 2(gre6)
			install_inout_port(connection, 1, [2])
			install_inout_port(connection, 2, [1])
		elif connection.dpid == str_to_dpid("ce-b8-5c-71-5e-4f"):
			# host-07 switch  1(tap0), 2(gre7)
			install_inout_port(connection, 1, [2])
			install_inout_port(connection, 2, [1])
		elif connection.dpid == str_to_dpid("4a-84-54-fd-db-43"):
			# host-08 switch  1(tap0), 2(gre8)
			install_inout_port(connection, 1, [2])
			install_inout_port(connection, 2, [1])
		elif connection.dpid == str_to_dpid("6a-59-d5-d4-92-44"):
			# host-09 switch  1(tap0), 2(gre9)
			install_inout_port(connection, 1, [2])
			install_inout_port(connection, 2, [1])
		elif connection.dpid == str_to_dpid("fe-92-3d-be-8c-47"):
			# host-10 switch  1(tap0), 2(gre10)
			install_inout_port(connection, 1, [2])
			install_inout_port(connection, 2, [1])
		elif connection.dpid == str_to_dpid("1a-ab-10-e1-c8-47"):
			# host-11 switch  1(tap0), 2(gre11)
			install_inout_port(connection, 1, [2])
			install_inout_port(connection, 2, [1])
		elif connection.dpid == str_to_dpid("fe-98-29-28-fa-4a"):
			# host-12 switch  1(tap0), 2(gre12)
			install_inout_port(connection, 1, [2])
			install_inout_port(connection, 2, [1])
		elif connection.dpid == str_to_dpid("9a-29-05-08-c0-47"):
			# host-13 switch  1(tap0), 2(gre13)
			install_inout_port(connection, 1, [2])
			install_inout_port(connection, 2, [1])
		elif connection.dpid == str_to_dpid("9a-f2-e1-da-9e-46"):
			# host-14 switch  1(tap0), 2(gre14)
			install_inout_port(connection, 1, [2])
			install_inout_port(connection, 2, [1])
		elif connection.dpid == str_to_dpid("8e-6c-82-fe-89-48"):
			# host-15 switch  1(tap0), 2(gre15)
			install_inout_port(connection, 1, [2])
			install_inout_port(connection, 2, [1])
		elif connection.dpid == str_to_dpid("6a-35-ac-ba-48-46"):
			# host-16 switch  1(tap0), 2(gre16)
			install_inout_port(connection, 1, [2])
			install_inout_port(connection, 2, [1])

	def _install_flow_bcast(self, mhst):
		for v in mhst._adj_matrix:
			if is_host(v):
				continue
			for u in mhst._adj_matrix[v]:
				# create flow for bcast packet 
				match = of.ofp_match()
				match.dl_type = pkt.ethernet.IP_TYPE
				match.in_port = mhst._adj_matrix[v][u]
				#match.dl_src = EthAddr("00:00:00:FF:FF:FF")
				match.dl_dst = EthAddr("00:00:00:FF:FF:FF")

				msg = of.ofp_flow_mod()
				msg.priority = 40000
				msg.match = match
				#msg.actions.append(of.ofp_action_dl_addr.set_src(EthAddr("00:00:00:FF:FF:FF")))
				for w in mhst._adj_matrix[v]:
					if w != u:
						out_port = mhst._adj_matrix[v][w]
						# if output link to host change dst mac addr to that machine
						#if is_host(w):
						#	dl_dst = self._host_list[w].mac_address
						#	msg.actions.append(of.ofp_action_dl_addr.set_dst(dl_dst))
						msg.actions.append(of.ofp_action_output(port = out_port))

				self._datapath_list[str_to_dpid(v)].connection.send(msg)

	def _install_flow_reduce_path(self, reduce_level, reduce_root):
		hex_root = hex(24)[2:].zfill(4)
		eth_addr = "00:00:00:0" + hex(reduce_level) + ":" + hex_root[:2] + ":" + hex_root[2:]
		dl_src = EthAddr(eth_addr)

		#for datapath
		#	for node in path
		#		create match
		#		match = of.ofp_match()
		#		create flow mod
		#		send flow to datapath

	def _install_flow_shortest_path(self, src_host, dst_host):
		src_host = str(src_host)
		dst_host = str(dst_host)

		(path, out_ports, in_ports) = self._host_host_path[src_host][dst_host]
		for dp, out_port, in_port in zip(path, out_ports, in_ports):
			
			#nw_tos|tp_dst|dl_dst|dl_src|in_port|dl_vlan_pcp|nw_proto|dl_vlan|tp_src|dl_type|nw_src(/0)|nw_dst(/0)

			# create flow for send src to dst in shortest path
			match = of.ofp_match()
			match.dl_type = pkt.ethernet.IP_TYPE
			match.in_port = in_port
			match.nw_src = IPAddr(src_host)
			match.nw_dst = IPAddr(dst_host)

			msg = of.ofp_flow_mod()
			msg.priority = 30000
			msg.match = match
			msg.actions.append(of.ofp_action_output(port = out_port))

			self._datapath_list[str_to_dpid(dp)].connection.send(msg)

			# special match that check source MAC address
			"""
			match2 = of.ofp_match()
			match2.dl_type = pkt.ethernet.IP_TYPE
			match2.in_port = in_port
			match2.nw_src = IPAddr(src_host)
			match2.nw_dst = IPAddr(dst_host)
			match2.dl_src = EthAddr("00:00:AA:FF:FF:FF")

			msg2 = of.ofp_flow_mod()
			msg2.priority = 30000 + 10000
			msg2.match = match2
			# more action here : http://www.noxrepo.org/_/nox-classic-doxygen/pyopenflow_8py.html
			msg2.actions.append(of.ofp_action_dl_addr.set_src(EthAddr("00:00:AA:FF:FF:FF")))
			msg2.actions.append(of.ofp_action_nw_addr.set_src(IPAddr(src_host)))
			msg2.actions.append(of.ofp_action_output(port = out_port))

			self._datapath_list[str_to_dpid(dp)].connection.send(msg)
			"""

	def _install_flow_detect_host_topology(self, connection):
		#http://pieknywidok.blogspot.jp/2012/08/arp-and-ping-in-pox-building-pox-based.html
		match = of.ofp_match()
		match.dl_type = pkt.ethernet.ARP_TYPE
		match.dl_dst = "\x00\x00\x00\x00\x00\x08" # TODO: remove magic number

		msg = of.ofp_flow_mod()
		msg.priority = 65000 # TODO: set to equal to LLDP priority
		msg.match = match
		msg.actions.append(of.ofp_action_output(port = of.OFPP_CONTROLLER))
		connection.send(msg)

	#def _construct_allreduce_plan(self):
		# find hosts group
		#for s in leaf_switch:
		#	for 

	def _construct_connected_graph(self):
		# add all hosts name to vertex in graph
		for host in self._host_list:
			self._conn_graph.add_vertex(host)
		# add link between host
		for src_h in self._host_list:
			for dst_h in self._host_list:
				if src_h == dst_h:
					continue
				# wait for compute shortest path
				while src_h not in self._host_host_path:
					pass
				while dst_h not in self._host_host_path[src_h]:
					pass
				(path, out_ports, in_ports) = self._host_host_path[src_h][dst_h]
				# TODO: fix weight by adding congestion
				weight = len(path) + 1
				self._conn_graph.add_edge(src_h, dst_h, weight)
				self._conn_graph.add_edge(dst_h, src_h, weight)

	def _construct_binomial_tree_and_plan(self, dump = False):
		# rank 0, 1, 2, ..., n-1
		vertex_num = self._conn_graph.get_vertex_num()
		binomial_t_of_root = {}
		for root in self._host_list:
			conn_graph = Graph()
			conn_graph._adj_matrix = self._conn_graph._adj_matrix.copy()
			conn_graph._vertex_count = self._conn_graph._vertex_count

			def create_binomial_tree(root_node, vertex_num, conn_graph, added_list):
				added_list[root_node._name] = 1

				if vertex_num == 1:
					return root_node
				else:
					v = vertex_num / 2
					while v >= 1:
						root_name = root_node._name
						(min_node, min_weight) = conn_graph.get_shortest_link_from(root_name, added_list)
						if min_node:
							# TODO: fix for non log2 tree
							sub_tree = create_binomial_tree(TreeNode(min_node), v, conn_graph, added_list)
							root_node.add_child(sub_tree)

						v = v / 2 # update

					return root_node

			def dump_binomail_tree(root_node):
				# print root -> [child0, child1, ... childn]
				node_str = ""
				node_str += str(self._host_name_to_rank(root_node._name)) + " -> ["
				for child in root_node._child:
					node_str += str(self._host_name_to_rank(child._name)) + ","
				node_str +=  "]"
				print node_str

				# recursive print
				for child in root_node._child:
					dump_binomail_tree(child)

			added_list = {}
			tree = create_binomial_tree(TreeNode(root), vertex_num, conn_graph, added_list)
			binomial_t_of_root[root] = tree

			"""
			if str(self._host_name_to_rank(root)) == "0":
				print "Binomail tree of root at " + root + ":"
				dump_binomail_tree(tree)
			"""

			def create_reduce_plan_and_remove_leaf(root_node, level, plan):
				if root_node.is_leaf():
					return None
				else:
					del_list = []
					for c in range(root_node.get_child_num()):
						if root_node._child[c].is_leaf():
							plan.append((level,root_node._child[c]._name,root_node._name))
							del_list.append(c)
						else:
							create_reduce_plan_and_remove_leaf(root_node._child[c], level, plan)
					# reverse delete list
					del_list = del_list[::-1]
					for d in del_list:
						del root_node._child[d]

			def create_reduce_plan(root_node):
				plan = []
				level = 1
				while root_node.get_child_num() > 0:
					create_reduce_plan_and_remove_leaf(root_node, level, plan)
					level += 1
				return plan

			self._reduce_plan[root] = create_reduce_plan(tree)

	def _add_link_to_graph(self, src_dpid, dst_dpid, src_port, dst_port):
		def cast_node_name(name):
			if str(name).find(".") >= 0: # host ip
				return str(name)
			else: #datapath
				return dpid_to_str(name)
		src_dpid = cast_node_name(src_dpid)
		dst_dpid = cast_node_name(dst_dpid)
		src_port = int(src_port)
		dst_port = int(dst_port)

		self._topo_graph.add_vertex(src_dpid)
		self._topo_graph.add_vertex(dst_dpid)
		self._topo_graph.add_edge(src_dpid, dst_dpid, src_port)
		self._topo_graph.add_edge(dst_dpid, src_dpid, dst_port)

	def _handle_openflow_discovery_LinkEvent(self, event):
		link = event.link
		self._link_num += 1
		print "Link num = " + str(self._link_num)
		self._add_link_to_graph(link.dpid1, link.dpid2, link.port1, link.port2)


class Graph(object):

	def __init__(self):
		self._adj_matrix = {}  # value is port number
		self._vertex_count = 0

	def add_vertex(self, vertex_name):
		if vertex_name not in self._adj_matrix:
			self._adj_matrix[vertex_name] = {}
			self._vertex_count += 1

	def add_edge(self, src, dst, value):
		self._adj_matrix[src][dst] = value

	def remove_edge(self, src, dst):
		del self._adj_matrix[src][dst]

	def get_shortest_link_from(self, from_node, add_list):
		min_node = None
		min_weight = float("inf")
		for dst_node in self._adj_matrix[from_node]:
			if dst_node not in add_list and self._adj_matrix[from_node][dst_node] < min_weight:
				min_weight = self._adj_matrix[from_node][dst_node]
				min_node = dst_node

		return (min_node, min_weight)

	def get_vertex_num(self):
		return len(self._adj_matrix)

	def get_vertex_list(self):
		return self._adj_matrix.keys()

	def get_value(self, src, dst):
		if src in self._adj_matrix:
			if dst in self._adj_matrix[src]:
				return self._adj_matrix[src][dst]
		if src in self._adj_matrix and src == dst:
			return 0;
		return float("inf")

	def get_root_value(self, node):
		adj_level = {}
		adj_level[node] = 0

		node_list = [node]

		while len(node_list) > 0:
			n = node_list.pop(0)
			for adj_n in self._adj_matrix[n]:
				if adj_n not in adj_level:
					node_list.append(adj_n)
					adj_level[adj_n] = adj_level[n] + 1

		host_min_dist = float("inf")
		for n in self._adj_matrix:
			if is_host(n) and host_min_dist > adj_level[n]:
				host_min_dist = adj_level[n]

		return host_min_dist

	def minimum_host_spanning_tree(self, dump = False):
		mst = self.spanning_tree()
		
		while True:
			delete = False
			# find non-host and 1 degree vertex, remove it
			for v in mst._adj_matrix:
				if is_datapath(v) and len(mst._adj_matrix[v]) == 1:
					u = mst._adj_matrix[v]
					del mst._adj_matrix[v]
					del mst._adj_matrix[u][v]
					delete = True

			if not delete:
				break

		if dump:
			print "Minimum Host Spanning Tree:"
			for v in mst._adj_matrix:
				node_str = v + " -> ["
				for u in mst._adj_matrix[v]:
					node_str += u + ", "
				node_str += "]"
				print node_str

		return mst

	# Reachable problem
	def spanning_tree(self):
		st = Graph()
		for v in self._adj_matrix:
			st._adj_matrix[v] = {}
		st._vertex_count = self._vertex_count

		s = set()
		queue = []
		s.add(self._adj_matrix.keys()[0])
		queue.append(self._adj_matrix.keys()[0])

		while len(s) < self._vertex_count:
			v = queue.pop(0)
			# add reachable from v to queue (reachable that not in s)
			for u in self._adj_matrix[v]:
				if u not in s:
					s.add(u)
					queue.append(u)
					st._adj_matrix[u][v] = self._adj_matrix[u][v]
					st._adj_matrix[v][u] = self._adj_matrix[v][u]

		return st

	def k_shortest_paths(self, from_node, to_node, k):

		from_node = str(from_node)
		to_node = str(to_node)

		def shortest_path(from_node, to_node):
			distance = {}
			previous = {}
			outport = {}
			inport = {}
			for node in self._adj_matrix:
				distance[node] = float("inf")
				previous[node] = None
				outport[node] = None
				inport[node] = None
			distance[from_node] = 0
			# TODO: use heap
			"""node_heap = []
			for node in self._adj_matrix:
				if node == from_node:
					heappush(node_heap, (node, 0.0))
				else:
					heappush(node_heap, (node, float("inf")))
			"""

			# run until ...
			all_node = distance.copy()
			while len(all_node) > 0:
				curr = min(all_node, key=all_node.get)

				for adj_node in self._adj_matrix[curr]:

					alt = distance[curr] + 1 #self._adj_matrix[curr][adj_node]
					if alt < distance[adj_node]:
						distance[adj_node] = all_node[adj_node] = alt
						previous[adj_node] = curr
						outport[adj_node] = self._adj_matrix[curr][adj_node]
						inport[adj_node] = self._adj_matrix[adj_node][curr]

				del all_node[curr]

			# create path
			solution_path = []
			out_ports = []
			in_ports = []
			curr = to_node
			while previous[curr] != None:
				solution_path.append(curr)
				out_ports.append(outport[curr])
				in_ports.append(inport[curr])
				curr = previous[curr]
			solution_path = solution_path[::-1]
			out_ports = out_ports[::-1]
			in_ports = in_ports[::-1]

			solution_path = solution_path[:-1]
			out_ports = out_ports[1:]
			in_ports = in_ports[:-1]

			return (solution_path, out_ports, in_ports, distance[to_node])
			# end shortest path function

		shortest_path_list = []

		# find first shortest path
		(path, outs, ins, shortest_path_distance) = shortest_path(from_node, to_node)
		shortest_path_list.append( (path, outs, ins) )
		first_path = list(path)

		for n in range(len(first_path)-1):
			from_n = first_path[n]
			to_n = first_path[n+1]

			tmp1 = self._adj_matrix[from_n][to_n]
			tmp2 = self._adj_matrix[to_n][from_n]

			# find another path
			(path, outs, ins, dist) = shortest_path(from_n, to_n)
			if dist == shortest_path_distance:
				shortest_path_list.append( (path, outs, ins) )

			self._adj_matrix[from_n][to_n] = tmp1
			self._adj_matrix[to_n][from_n] = tmp2

			if len(shortest_path_list) >= k:
				break

		print "Shortest path from [",from_node,"] to [",to_node,"] is"
		for (path, outs, ins) in shortest_path_list:
			print '[',','.join([dpid_to_switch_name(x) for x in path]),']'
			#print str(outs)
			#print str(ins)

		return shortest_path_list

class TreeNode(object):
	def __init__(self, name):
		self._name = name
		self._child = []

	def add_child(self, child):
		self._child.append(child)

	def get_child_num(self):
		return len(self._child)

	def is_leaf(self):
		return self.get_child_num() == 0

class NetworkHost():
	def __init__(self):
		self.private_ip = None
		self.public_ip = None
		self.mac_address = None
		self.adjacent_datapath = None
		self.sock = None
		self.rank = -1

class NetworkDatapath(object):
	def __init__(self):
		self.dpid = ""
		self.name = ""
		self.root_value = float("inf")


def launch():
	log.debug("SDN MPI component start")
	core.registerNew(MyComponent)