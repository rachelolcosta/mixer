import logging
import argparse
import socket
import select
import threading
from typing import Tuple, List, Mapping, Union, ValuesView

import mixer.broadcaster.cli_utils as cli_utils
import mixer.broadcaster.common as common

BINDING_HOST = ""
SHUTDOWN = False

logger = logging.getLogger() if __name__ == "__main__" else logging.getLogger(__name__)


class Connection:
    """ Represent a connection with a client """

    def __init__(self, server: "Server", socket: socket.socket, address):
        self.socket = socket
        self.address = address
        self.metadata = {}  # metadata are used between clients, but not by the server
        self.room: "Room" = None
        # TODO use a Queue and drop the mutex ?
        self.commands = []  # Pending commands to send to the client
        self._server = server
        # optimization to avoid too much messages when broadcasting client/room metadata updates
        self.list_all_clients_flag = False
        self.list_rooms_flag = False

    def start(self):
        self.thread = threading.Thread(None, self.run)
        self.thread.start()

    def join_room(self, room_name: str):
        error = None
        if self.room is not None:
            error = f"Received join_room({room_name}) but room {self.room.name} is already joined"
        if error:
            logger.warning(error)
            self.send_error(error)
            return

        with common.mutex:
            self._server.join_room(self, room_name)

    def leave_room(self, room_name: str):
        error = None
        if self.room is None:
            error = f"Received leave_room({room_name}) but no room is joined"
        elif room_name != self.room.name:
            error = f"Received leave_room({room_name}) but room {self.room.name} is joined instead"
        if error:
            logger.warning(error)
            self.send_error(error)
            return

        with common.mutex:
            self._server.leave_room(self, room_name)

    def clear_room(self, room_name: str):
        error = None
        if self.room is None:
            error = f"Received clear_room({room_name}) but no room is joined"
        elif room_name != self.room.name:
            error = f"Received clear_room({room_name}) but room {self.room.name} is joined instead"
        if error:
            logger.warning(error)
            self.send_error(error)
            return

        with common.mutex:
            room = self._server.get_room(room_name)
            if room is not None:
                room.clear()

    def send_list_rooms(self):
        self.list_rooms_flag = True
        return

    def send_client_ids(self):
        self.list_all_clients_flag = True
        return

    # todo check if still useful, and remove + refactor cli.py if not
    def send_list_room_clients(self, room_name: str = None, client_ids: Union[Mapping, List[Mapping]] = None):
        logger.debug("send_list_room_clients")
        with common.mutex:
            # ensure we use only one since each message ovewrites the previous one
            # on the client
            assert bool(room_name is not None) != bool(client_ids is not None)
            command = None

            if client_ids is not None:
                client_ids = client_ids if isinstance(client_ids, list) else [client_ids]
                ids = common.encode_json(client_ids)
                command = common.Command(common.MessageType.LIST_ROOM_CLIENTS, ids)

            if room_name is not None:
                room = self._server.get_room(room_name)
                if room is not None:
                    ids = common.encode_json(room.client_ids())
                    command = common.Command(common.MessageType.LIST_ROOM_CLIENTS, ids)
                else:
                    command = common.Command(
                        common.MessageType.SEND_ERROR, common.encode_string(f"No room named {room_name}.")
                    )
            if command:
                self.commands.append(command)

    # todo check if still useful, and remove + refactor cli.py if not
    def send_list_clients(self):
        """
        Joined clients for all rooms
        """
        with common.mutex:
            clients = []
            for room in self._server.rooms():
                clients.extend(room.client_ids())
            command = common.Command(common.MessageType.LIST_CLIENTS, common.encode_json(clients))
            self.commands.append(command)

    def get_list_all_clients_command(self):
        client_ids = self._server.client_ids()
        for client in client_ids:
            if (client[common.ClientMetadata.IP], client[common.ClientMetadata.PORT]) == self.address:
                client[common.ClientMetadata.IS_ME] = True
                break
        return common.Command(common.MessageType.LIST_ALL_CLIENTS, common.encode_json(client_ids))

    def client_id(self) -> Mapping[str, str]:
        return {
            **self.metadata,
            common.ClientMetadata.IS_ME: False,  # Will be changed before sending in self.send_client_ids()
            common.ClientMetadata.IP: self.address[0],
            common.ClientMetadata.PORT: self.address[1],
            common.ClientMetadata.ROOM: self.room.name if self.room is not None else None,
        }

    def set_client_metadata(self, metadata: dict):
        for key, value in metadata.items():
            self.metadata[key] = value
        self._server.broadcast_user_list()

    def send_error(self, s: str):
        logger.debug("Sending error %s", s)
        command = common.Command(common.MessageType.SEND_ERROR, common.encode_string(s))
        with common.mutex:
            self.commands.append(command)

    def on_client_disconnected(self):
        self._server.broadcast_user_list()

    def run(self):
        global SHUTDOWN
        while not SHUTDOWN:
            try:
                command = common.read_message(self.socket)
            except common.ClientDisconnectedException:
                break

            if command is not None:
                if command.type not in (common.MessageType.SET_CLIENT_METADATA, common.MessageType.LIST_ROOMS,):
                    logger.debug("Received from %s:%s - %s", self.address[0], self.address[1], command.type)

                if command.type == common.MessageType.JOIN_ROOM:
                    self.join_room(command.data.decode())

                elif command.type == common.MessageType.LEAVE_ROOM:
                    self.leave_room(command.data.decode())

                elif command.type == common.MessageType.LIST_ROOMS:
                    self.send_list_rooms()

                elif command.type == common.MessageType.DELETE_ROOM:
                    self._server.delete_room(command.data.decode())

                elif command.type == common.MessageType.CLEAR_ROOM:
                    self.clear_room(command.data.decode())

                elif command.type == common.MessageType.SET_CLIENT_NAME:
                    self.set_client_metadata({common.ClientMetadata.USERNAME: command.data.decode()})

                elif command.type == common.MessageType.LIST_ROOM_CLIENTS:
                    self.send_list_room_clients(room_name=command.data.decode())

                elif command.type == common.MessageType.LIST_ALL_CLIENTS:
                    self.send_client_ids()

                elif command.type == common.MessageType.LIST_CLIENTS:
                    self.send_list_clients()

                elif command.type == common.MessageType.SET_CLIENT_METADATA:
                    self.set_client_metadata(common.decode_json(command.data, 0)[0])

                elif command.type == common.MessageType.SET_ROOM_METADATA:
                    room_name, offset = common.decode_string(command.data, 0)
                    metadata, _ = common.decode_json(command.data, offset)
                    self._server.set_room_metadata(room_name, metadata)

                elif command.type == common.MessageType.SET_ROOM_KEEP_OPEN:
                    room_name, offset = common.decode_string(command.data, 0)
                    value, _ = common.decode_bool(command.data, offset)
                    self._server.set_room_keep_open(room_name, value)

                # Other commands
                elif command.type.value > common.MessageType.COMMAND.value:
                    if self.room is not None:
                        self.room.add_command(command, self)
                    else:
                        logger.warning(
                            "%s:%s - %s received but no room was joined",
                            self.address[0],
                            self.address[1],
                            command.type.value,
                        )

            try:
                if len(self.commands) > 0:
                    with common.mutex:
                        for command in self.commands:
                            if command.type not in (common.MessageType.LIST_ALL_CLIENTS,):
                                logger.debug("Sending to %s:%s - %s", self.address[0], self.address[1], command.type)
                            common.write_message(self.socket, command)
                        self.commands = []
                if self.list_all_clients_flag:
                    common.write_message(self.socket, self.get_list_all_clients_command())
                    self.list_all_clients_flag = False

                if self.list_rooms_flag:
                    common.write_message(self.socket, self._server.get_list_rooms_command())
                    self.list_rooms_flag = False
            except common.ClientDisconnectedException:
                break

        self.close()
        self.on_client_disconnected()

    def add_command(self, command):
        self.commands.append(command)

    def close(self):
        # called on disconnection
        if self.room is not None:
            self.room.remove_client(self)
        else:
            self._server.remove_unjoined_client(self)

        try:
            self.socket.close()
        except Exception:
            pass
        logger.info("%s closed", self.address)


class Room:
    """
    Room class is responsible for:
    - handling its list of clients (as Connection instances)
    - keep a list of commands, to be dispatched to new clients
    - dispatch added commands to already clients already in the room
    """

    def __init__(self, server: "Server", room_name: str):
        self.name = room_name
        self.keep_open = False  # Should the room remain open when no more clients are inside ?
        self._connections: List["Connection"] = []
        self.commands = []
        self.metadata = {}  # metadata are used between clients, but not by the server
        self._server: "Server" = server

    def client_count(self):
        return len(self._connections)

    def add_client(self, connection: Connection, first_client: bool):
        logger.info(f"Add Client {connection.address} to Room {self.name}")
        self._connections.append(connection)
        connection.room = self
        if first_client:
            connection.add_command(common.Command(common.MessageType.CONTENT))
        else:
            connection.add_command(common.Command(common.MessageType.CLEAR_CONTENT))

            for command in self.commands:
                connection.add_command(command)

    def client_ids(self):
        if not self._connections:
            return None
        return [c.client_id() for c in self._connections]

    def close(self):
        command = common.Command(common.MessageType.LEAVE_ROOM, common.encode_string(self.name))
        self.add_command(command, None)
        self._connections = []
        self._server.delete_room(self.name)

    def clear(self):
        self.commands = []

    def remove_client(self, connection: Connection):
        logger.info("Remove Client % s from Room % s", connection.address, self.name)
        self._connections.remove(connection)
        connection.room = None
        if self.client_count() == 0 and not self.keep_open:
            self._server.delete_room(self.name)
            logger.info('No more clients in room "%s". Room deleted', self.name)
        else:
            logger.info(f"Connections left : {self.client_count()}.")

    def merge_commands(self, command):
        command_type = command.type
        if command_type.value > common.MessageType.OPTIMIZED_COMMANDS.value:
            command_path = common.decode_string(command.data, 0)[0]
            if len(self.commands) > 0:
                stored_command = self.commands[-1]
                if (
                    command_type == stored_command.type
                    and command_path == common.decode_string(stored_command.data, 0)[0]
                ):
                    self.commands.pop()
        self.commands.append(command)

    def add_command(self, command, sender):
        with common.mutex:
            self.merge_commands(command)
            for connection in self._connections:
                if connection != sender:
                    connection.add_command(command)


class Server:
    def __init__(self):
        Address = Tuple[str, str]  # noqa
        self._rooms: Mapping[str, Room] = {}
        # Connections not joined to any room
        self._unjoined_connections: Mapping[Address, Connection] = {}
        self._shutdown = False

    def shutdown(self):
        # mostly for tests
        self._shutdown = True

    def client_count(self):
        """
        Returns (numver of joined connections, number of unjoined connections)
        """
        joined = 0
        for room in self._rooms.values():
            joined += room.client_count()
        unjoined = len(self._unjoined_connections)
        return (joined, unjoined)

    def remove_unjoined_client(self, connection: Connection):
        with common.mutex:
            logger.debug("Server : removing unjoined client %s", connection.address)
            del self._unjoined_connections[connection.address]

    def get_room(self, room_name: str) -> Room:
        return self._rooms.get(room_name)

    def add_room(self, room_name: str) -> Room:
        with common.mutex:
            if room_name in self._rooms:
                raise ValueError(f"add_room: room with name {room_name} already exists")
            room = Room(self, room_name)
            self._rooms[room_name] = room
            logger.info(f"Room {room_name} added")
            self.broadcast_room_list()
            return room

    def delete_room(self, room_name: str):
        with common.mutex:
            if room_name not in self._rooms:
                logger.warning("Room %s does not exist.", room_name)
                return
            if self._rooms[room_name].client_count() > 0:
                logger.warning("Room %s is not empty.", room_name)
                return
            del self._rooms[room_name]
            logger.info(f"Room {room_name} deleted")

            self.broadcast_room_list()

    def join_room(self, connection: Connection, room_name: str):
        with common.mutex:
            assert connection.room is None
            room = self.get_room(room_name)
            first_client = False
            if room is None:
                logger.info(f"Room {room_name} does not exist. Creating it.")
                room = self.add_room(room_name)
                first_client = True

            peer = connection.address
            if peer in self._unjoined_connections:
                logger.debug("Reusing connection %s", peer)
                del self._unjoined_connections[peer]

            room.add_client(connection, first_client)
            self.broadcast_user_list()

    def leave_room(self, connection: Connection, room_name: str):
        with common.mutex:
            room = self.get_room(room_name)
            if room is None:
                raise ValueError(f"Room not found {room_name})")
            room.remove_client(connection)
            peer = connection.address
            assert peer not in self._unjoined_connections
            self._unjoined_connections[peer] = connection
            self.broadcast_user_list()

    def rooms_names(self) -> List[str]:
        return self._rooms.keys()

    def rooms(self) -> ValuesView[Room]:
        return self._rooms.values()

    def client_ids(self) -> List[Mapping]:
        with common.mutex:
            # gather all client ids
            client_ids = []
            for connection in self._unjoined_connections.values():
                client_ids.append(connection.client_id())
            for room in self._rooms.values():
                ids = room.client_ids()
                if ids is not None:
                    client_ids.extend(ids)
            return client_ids

    def all_connections(self) -> List[Connection]:
        with common.mutex:
            connections = list(self._unjoined_connections.values())
            for room in self._rooms.values():
                connections += room._connections
            return connections

    def broadcast_user_list(self):
        """
        Broadcast the list of all joined and unjoined clients to all
        joined and unjoined clients.

        This is called for every connection/join/client name change
        """
        with common.mutex:
            for connection in self.all_connections():
                connection.send_client_ids()

    def broadcast_room_list(self):
        with common.mutex:
            for connection in self.all_connections():
                connection.send_list_rooms()

    def set_room_metadata(self, room_name: str, metadata: dict):
        with common.mutex:
            if room_name not in self._rooms:
                logger.warning("Room %s does not exist.", room_name)
                return
            for key, value in metadata.items():
                self._rooms[room_name].metadata[key] = value
            self.broadcast_room_list()

    def set_room_keep_open(self, room_name: str, value: bool):
        with common.mutex:
            if room_name not in self._rooms:
                logger.warning("Room %s does not exist.", room_name)
                return
            self._rooms[room_name].keep_open = value
            self.broadcast_room_list()

    def get_list_rooms_command(self) -> common.Command:
        with common.mutex:
            result_dict = {}
            for room, value in self._rooms.items():
                result_dict[room] = {**value.metadata, "keep_open": value.keep_open}
            return common.Command(common.MessageType.LIST_ROOMS, common.encode_json(result_dict))

    def run(self, port):
        global SHUTDOWN
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind((BINDING_HOST, port))
        sock.setblocking(0)
        sock.listen(1000)

        logger.info("Listening on port % s", port)
        while not self._shutdown:
            try:
                timeout = 0.1  # Check for a new client every 10th of a second
                readable, _, _ = select.select([sock], [], [], timeout)
                if len(readable) > 0:
                    client_socket, client_address = sock.accept()
                    connection = Connection(self, client_socket, client_address)
                    assert connection.address not in self._unjoined_connections
                    self._unjoined_connections[connection.address] = connection
                    connection.start()
                    logger.info(f"New connection from {client_address}")

                    # Let the new client know the room and user lists
                    self.broadcast_user_list()
            except KeyboardInterrupt:
                self.shutdown()

        logger.info("Shutting down server")
        SHUTDOWN = True
        sock.close()


def main():
    args, args_parser = parse_cli_args()
    cli_utils.init_logging(args)

    server = Server()
    server.run(args.port)


def parse_cli_args():
    parser = argparse.ArgumentParser(description="Start broadcasting server for Mixer")
    cli_utils.add_logging_cli_args(parser)
    parser.add_argument("--port", type=int, default=common.DEFAULT_PORT)
    return parser.parse_args(), parser


if __name__ == "__main__":
    main()
