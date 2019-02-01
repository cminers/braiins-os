# Copyright (C) 2018  Braiins Systems s.r.o.
#
# This file is part of Braiins Build System (BB).
#
# BB is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import hashlib


class HashStream:
    """
    Class for computing hash during stream reading

    Result digest is stored in hash attributed.
    It is computed from all blocks which has been read from the stream.
    """
    def __init__(self, stream, algorithm):
        """
        Create new stream which support hashing of original stream with given algorithm

        :param stream:
            Original stream used for reading and continual hashing.
        :param algorithm:
            The name of hash algorithm supported by hashlib module.
        """
        self._stream = stream
        self.hash = hashlib.new(algorithm)

    def read(self, size):
        """
        Read from original stream block of specified size and compute its hash

        :param size:
            Size of block to be read from original stream.
        :return:
            Read block of specified size.
        """
        block = self._stream.read(size)
        self.hash.update(block)
        return block

    def close(self):
        """
        Close original stream.
        """
        self._stream.close()
