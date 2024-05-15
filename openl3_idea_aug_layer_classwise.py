from tensorflow.keras import backend as K
from tensorflow.keras import layers
import tensorflow as tf

class AugLayer(layers.Layer):
    def __init__(self, prob, **kwargs):
        super(AugLayer, self).__init__(**kwargs)
        self.prob = prob

    def build(self, input_shape):
        self.built = True

    def call(self, inputs, training=None):
        # mixup data
        X1 = inputs[0]
        X1_rev = tf.reverse(inputs[0], axis=[0])

        # mixup labels
        y = tf.concat([inputs[2], tf.zeros_like(inputs[2]), tf.zeros_like(inputs[2])], axis=1)
        y_rev = tf.concat([tf.zeros_like(inputs[2]), 0.5 * inputs[2], 0.5 * tf.reverse(inputs[2], axis=[0])], axis=1)  # best?

        # apply mixup or not
        dec = tf.dtypes.cast(tf.random.uniform(shape=[tf.shape(inputs[0])[0]]) < self.prob, tf.dtypes.float32)
        dec1 = tf.reshape(dec, [-1] + [1] * (len(inputs[0].shape) - 1))
        out1 = dec1 * X1 + (1 - dec1) * X1_rev
        dec2 = tf.reshape(dec, [-1] + [1] * (len(y.shape) - 1))
        out2 = dec2 * y + (1 - dec2) * y_rev
        outputs = [out1, inputs[1], out2]

        # pick output corresponding to training phase
        return K.in_train_phase(outputs, [inputs[0], inputs[1], y], training=training)

    def get_config(self):
        config = {
            'prob': self.prob,
        }
        base_config = super(AugLayer, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

