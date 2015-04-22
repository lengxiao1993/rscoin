from collections import namedtuple
from struct import pack, unpack

from hashlib import sha256

from petlib.ec import EcGroup, EcPt
from petlib.bn import Bn
from petlib.ecdsa import do_ecdsa_sign, do_ecdsa_verify


# Named structures
InputTx = namedtuple('InputTx', ['tx_id', 'pos'])
OutputTx = namedtuple('OutputTx', ['key_id', 'value'])
UtxoDiff = namedtuple('UtxoDIff', ['to_add', 'to_del'])

_globalECG = EcGroup()

class Key:
    """ Represents a key pair (or just a public key)"""

    def __init__(self, key_bytes, public=True):
        """ Make a key given a public or private key in bytes """
        self.G = _globalECG
        if public:
            self.sec = None
            self.pub = EcPt.from_binary(key_bytes, self.G)
        else:
            self.sec = Bn.from_binary(key_bytes)
            self.pub = self.sec * self.G.generator()

    def sign(self, message):
        """ Sign a 32-byte message using the key pair """

        assert len(message) == 32
        assert self.sec is not None
        r, s = do_ecdsa_sign(self.G, self.sec, message)
        r0, s0 = r.binary(), s.binary()
        assert len(r0) <= 32 and len(s0) <= 32
        sig = pack("H32sH32s", len(r0), r0, len(s0), s0)
        return sig

    def verify(self, message, sig):
        """ Verify the message and the signature """

        assert len(message) == 32
        lr, r, ls, s = unpack("H32sH32s", sig)
        sig = Bn.from_binary(r[:lr]), Bn.from_binary(s[:ls])
        return do_ecdsa_verify(self.G, self.pub, sig, message)

    def id(self):
        """ The fingerprint of the public key """

        return sha256(self.pub.export()).digest()

    def export(self):
        """ Export the public and secret keys as strings """

        sec = None
        if self.sec is not None:
            sec = self.sec.binary()
        return (self.pub.export(), sec)


class Tx:
    """ Represents a transaction """

    def __init__(self, inTx=[], outTx=[]):
        """ Initialize a transaction """
        self.inTx = inTx
        self.outTx = outTx

    def serialize(self):
        """ Turn this transaction into a cannonical byte string """

        ser = pack("HH", len(self.inTx), len(self.outTx))

        # Serialize the In transations
        for intx in self.inTx:
            ser += pack("32sI", intx.tx_id, intx.pos)

        # Serialize the Out transactions
        for outtx in self.outTx:
            ser += pack("32sQ", outtx.key_id, outtx.value)

        return ser

    def __eq__(self, other):
        return isinstance(other, Tx) and self.id() == other.id()

    def __ne__(self, other):
        return not (self == other)

    @staticmethod
    def parse(data):
        """ Parse a serialized transaction """
        Lin, Lout = unpack("HH", data[:4])
        data = data[4:]

        inTx = []
        for _ in range(Lin):
            idx, posx = unpack("32sI", data[:32+4])
            inTx += [InputTx(idx, posx)]
            data = data[32+4:]

        outTx = []
        for _ in range(Lout):
            kidx, valx = unpack("32sQ", data[:32+8])
            outTx += [OutputTx(kidx, valx)]
            data = data[32+8:]

        return Tx(inTx, outTx)

    def id(self):
        """ Return the fingerprint of the tranaction """
        return sha256(self.serialize()).digest()

    def get_utxo_in_keys(self):
        """ Returns the entries for the utxo for valid transactions """

        in_utxo = []
        # Package the inputs
        for intx in self.inTx:
            inkey = pack("32sI", intx.tx_id, intx.pos)
            in_utxo += [inkey]
        return in_utxo

    def get_utxo_out_entries(self):
        """ Returns the entries for the utxo for valid transactions """
    
        # Serialize the Out transactions
        out_utxo = []
        for pos, outtx in enumerate(self.outTx):
            outkey = pack("32sI", self.id(), pos)
            outvalue = pack("32sQ", outtx.key_id, outtx.value)
            out_utxo += [(outkey, outvalue)]

        return out_utxo


    def check_transaction(self, past_tx, keys, sigs, masterkey=None):
        """ Checks that a transaction is valid given evidence """

        # Coin generation / Issuing transaction
        all_good = True
        if len(past_tx) == 0:
            all_good &= (len(past_tx) == len(self.inTx) == 0)
            all_good &= (len(keys) == len(sigs) == 1)
            all_good &= (keys[0] == masterkey)

            k = Key(keys[0])
            all_good &= k.verify(self.id(), sigs[0])
            return all_good

        # Other transcations
        all_good &= (len(past_tx) == len(keys) == len(sigs) == len(self.inTx))
        if not all_good:
            return False

        out_utxo = []
        for (otx, txin) in zip(past_tx, self.inTx):
            # Check the transaction ID matches
            oTx = Tx.parse(otx)

            all_good &= (txin.tx_id == oTx.id())
            all_good &= (0 <= txin.pos < len(oTx.outTx))

            outtx = oTx.outTx[txin.pos]
            out_utxo += [(txin.tx_id, txin.pos, outtx.key_id, outtx.value)]

        return self.check_transaction_utxo(out_utxo, keys, sigs, masterkey=None)



    def check_transaction_utxo(self, past_utxo, keys, sigs, masterkey=None):
        """ Checks that a transaction is valid given evidence """

        all_good = True

        ## Special issuing transaction -- has no parents
        if len(past_utxo) == 0:
            all_good &= (len(past_utxo) == len(self.inTx) == 0)
            all_good &= (len(keys) == len(sigs) == 1)
            all_good &= (keys[0] == masterkey)

            k = Key(keys[0])
            all_good &= k.verify(self.id(), sigs[0])
            return all_good


        ## Normal transaction has parents
        all_good &= len(past_utxo) > 0
        all_good &= (len(past_utxo) == len(keys) == len(sigs) == len(self.inTx))
        if not all_good:
            return False            

        val = 0
        for (utxo, okey, osig, txin) in zip(past_utxo, keys, sigs, self.inTx):
            (txid, txpos, txkeyid, txval) = utxo

            # Check the transaction ID matches
            all_good &= (txid == txin.tx_id)
            all_good &= (txpos == txin.pos)
            if not all_good:
                return False

            # Check the key matches
            k = Key(okey)
            all_good &= (txkeyid == k.id())
            val += txval

            # Check the signature matches
            all_good &= k.verify(self.id(), osig)

        # Check the value matches
        total_value = sum([o.value for o in self.outTx])
        all_good &= (val == total_value)

        return all_good
