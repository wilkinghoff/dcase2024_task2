import pandas as pd
import numpy as np
import keras
import os
import soundfile as sf
import tensorflow as tf
import librosa
from sklearn.metrics import roc_auc_score
from tqdm import tqdm
from sklearn.preprocessing import LabelEncoder
from mixup_layer import MixupLayer
from feature_exchange import AugLayer
from subcluster_adacos import SCAdaCos, AdaProj
from scipy.stats import hmean
from tensorflow.keras import backend as K
from sklearn.cluster import KMeans
from scipy.spatial.distance import cdist
import tensorflow_probability as tfp
from sklearn.utils import class_weight


def adjust_size(wav, new_size):
    reps = int(np.ceil(new_size/wav.shape[0]))
    offset = np.random.randint(low=0, high=int(reps*wav.shape[0]-new_size+1))
    new_wav = librosa.util.pad_center(wav, new_size)
    return new_wav
    
    
def temporal_mean(spec, keepdims=False):
    # take average over time but do not consider zeros resulting from padding waveform
    norm = tf.where(spec>0, tf.ones_like(spec), tf.zeros_like(spec))
    norm = tf.reduce_sum(norm, axis=2, keepdims=True)+1e-16
    return tf.reduce_sum(spec/norm, axis=1, keepdims=keepdims)


class GetWelch(tf.keras.layers.Layer):
    def __init__(self, nperseg=4096, noverlap=2048):
        super(GetWelch, self).__init__()
        self.nperseg = nperseg
        self.noverlap = noverlap

    def build(self, input_shape):
        super(GetWelch, self).build(input_shape)

    def call(self, waveform):
        # Compute the spectrogram
        stfts = tf.signal.stft(waveform, frame_length=self.nperseg, frame_step=self.nperseg - self.noverlap, fft_length=self.nperseg)
        spectrograms = tf.abs(stfts)

        # Power spectrogram
        Sxx = tf.square(spectrograms)

        # Average over time
        Sxx = temporal_mean(Sxx)

        # Logarithmic scaling
        EPS = 1e-16
        Sxx = tf.math.log(Sxx + EPS)

        return Sxx#tf.squeeze(Sxx, axis=0)

    def get_config(self):
        config = {
            'nperseg': self.nperseg,
            'noverlap': self.noverlap
        }
        config.update(super(GetWelch, self).get_config())
        return config


class MagnitudeSpectrogram(tf.keras.layers.Layer):
    """
    Compute magnitude spectrograms.
    https://towardsdatascience.com/how-to-easily-process-audio-on-your-gpu-with-tensorflow-2d9d91360f06
    """

    def __init__(self, sample_rate, fft_size, hop_size, **kwargs):
        super(MagnitudeSpectrogram, self).__init__(**kwargs)
        self.sample_rate = sample_rate
        self.fft_size = fft_size
        self.hop_size = hop_size

    def build(self, input_shape):
        super(MagnitudeSpectrogram, self).build(input_shape)

    def call(self, waveforms):
        spectrograms = tf.signal.stft(waveforms,
                                      frame_length=self.fft_size,
                                      frame_step=self.hop_size,
                                      pad_end=False)
        magnitude_spectrograms = tf.abs(spectrograms)
        magnitude_spectrograms = tf.expand_dims(magnitude_spectrograms, 3)
        return magnitude_spectrograms

    def get_config(self):
        config = {
            'fft_size': self.fft_size,
            'hop_size': self.hop_size,
            'sample_rate': self.sample_rate
        }
        config.update(super(MagnitudeSpectrogram, self).get_config())
        return config

def mixupLoss(y_true, y_pred):
    target = y_pred[:, :, 1]  # mixed-up labels
    output = y_pred[:, :, 0]  # mixed-up predictions
    return tf.keras.losses.categorical_crossentropy(target, output)


def length_norm(mat):
    norm_mat = []
    for line in mat:
        temp = line / np.math.sqrt(sum(np.power(line, 2)))
        norm_mat.append(temp)
    norm_mat = np.array(norm_mat)
    return norm_mat


def model_emb_cnn(num_classes, raw_dim, n_subclusters, use_bias=False):
    data_input = tf.keras.layers.Input(shape=(raw_dim, 1), dtype='float32')
    label_input = tf.keras.layers.Input(shape=(num_classes,), dtype='float32')
    y = label_input
    x = data_input
    l2_weight_decay = tf.keras.regularizers.l2(1e-5)
    x_mix = x
    x_mix, y_mix = MixupLayer(prob=0.5)([x, y])

    # FFT
    #x = tf.keras.layers.Lambda(lambda x: tf.math.abs(tf.signal.fft(tf.complex(x[:, :, 0], tf.zeros_like(x[:, :, 0])))[:, :int(raw_dim / 2)]))(x_mix)
    x = tf.keras.layers.Lambda(lambda x: tf.math.abs(tf.signal.fft(tf.complex(x[:, :, 0], tf.zeros_like(x[:, :, 0])))[:, :8000]))(x_mix)  # should one use a zero filter here too?
    
    #x = tf.keras.layers.Reshape((raw_dim,))(x_mix)
    #x = GetWelch()(x)
    x = tf.keras.layers.Reshape((-1,1))(x)
    x = tf.keras.layers.Conv1D(128, 256, strides=64, activation='linear', padding='same',
                               kernel_regularizer=l2_weight_decay, use_bias=use_bias)(x)
    x = tf.keras.layers.ReLU()(x)
    x = tf.keras.layers.Conv1D(128, 64, strides=32, activation='linear', padding='same',
                               kernel_regularizer=l2_weight_decay, use_bias=use_bias)(x)
    x = tf.keras.layers.ReLU()(x)
    x = tf.keras.layers.Conv1D(128, 16, strides=4, activation='linear', padding='same',
                               kernel_regularizer=l2_weight_decay, use_bias=use_bias)(x)
    x = tf.keras.layers.ReLU()(x)

    x = tf.keras.layers.Flatten()(x)
    x = tf.keras.layers.Dense(128, kernel_regularizer=l2_weight_decay, use_bias=use_bias)(x)
    x = tf.keras.layers.BatchNormalization()(x)
    x = tf.keras.layers.ReLU()(x)
    x = tf.keras.layers.Dense(128, kernel_regularizer=l2_weight_decay, use_bias=use_bias)(x)
    x = tf.keras.layers.BatchNormalization()(x)
    x = tf.keras.layers.ReLU()(x)
    x = tf.keras.layers.Dense(128, kernel_regularizer=l2_weight_decay, use_bias=use_bias)(x)
    x = tf.keras.layers.BatchNormalization()(x)
    x = tf.keras.layers.ReLU()(x)
    x = tf.keras.layers.Dense(128, kernel_regularizer=l2_weight_decay, use_bias=use_bias)(x)
    x = tf.keras.layers.BatchNormalization()(x)
    x = tf.keras.layers.ReLU()(x)

    emb_fft = tf.keras.layers.Dense(256, name='emb_fft', kernel_regularizer=l2_weight_decay, use_bias=use_bias)(x)

    # magnitude
    x = tf.keras.layers.Reshape((raw_dim,))(x_mix)
    x = MagnitudeSpectrogram(16000, 1024, 512)(x)

    x = tf.keras.layers.Lambda(lambda x: x-temporal_mean(x, keepdims=True))(x) # CMN-like normalization
    x = tf.keras.layers.BatchNormalization(axis=-2)(x)

    # first block
    x = tf.keras.layers.Conv2D(16, 7, strides=2, activation='linear', padding='same',
                               kernel_regularizer=l2_weight_decay, use_bias=use_bias)(x)
    x = tf.keras.layers.BatchNormalization()(x)
    x = tf.keras.layers.ReLU()(x)
    x = tf.keras.layers.MaxPooling2D(3, strides=2)(x)

    # second block
    xr = tf.keras.layers.ReLU()(x)
    xr = tf.keras.layers.Conv2D(16, 3, activation='linear', padding='same', kernel_regularizer=l2_weight_decay,
                                use_bias=use_bias)(xr)
    xr = tf.keras.layers.BatchNormalization()(xr)
    xr = tf.keras.layers.ReLU()(xr)
    xr = tf.keras.layers.Conv2D(16, 3, activation='linear', padding='same', kernel_regularizer=l2_weight_decay,
                                use_bias=use_bias)(xr)
    x = tf.keras.layers.Add()([x, xr])
    x = tf.keras.layers.BatchNormalization()(x)
    xr = tf.keras.layers.ReLU()(x)
    xr = tf.keras.layers.Conv2D(16, 3, activation='linear', padding='same', kernel_regularizer=l2_weight_decay,
                                use_bias=use_bias)(xr)
    xr = tf.keras.layers.ReLU()(xr)
    xr = tf.keras.layers.BatchNormalization()(xr)
    xr = tf.keras.layers.Conv2D(16, 3, activation='linear', padding='same', kernel_regularizer=l2_weight_decay,
                                use_bias=use_bias)(xr)
    x = tf.keras.layers.Add()([x, xr])

    # third block
    x = tf.keras.layers.BatchNormalization()(x)
    xr = tf.keras.layers.ReLU()(x)
    xr = tf.keras.layers.Conv2D(32, 3, strides=(2, 2), activation='linear', padding='same',
                                kernel_regularizer=l2_weight_decay, use_bias=use_bias)(xr)
    xr = tf.keras.layers.BatchNormalization()(xr)
    xr = tf.keras.layers.ReLU()(xr)
    xr = tf.keras.layers.Conv2D(32, 3, activation='linear', padding='same', kernel_regularizer=l2_weight_decay,
                                use_bias=use_bias)(xr)
    x = tf.keras.layers.MaxPooling2D((2, 2), padding='same')(x)
    x = tf.keras.layers.Conv2D(kernel_size=1, filters=32, strides=1, padding="same",
                               kernel_regularizer=l2_weight_decay, use_bias=use_bias)(x)
    x = tf.keras.layers.Add()([x, xr])
    x = tf.keras.layers.BatchNormalization()(x)
    xr = tf.keras.layers.ReLU()(x)
    xr = tf.keras.layers.Conv2D(32, 3, activation='linear', padding='same', kernel_regularizer=l2_weight_decay,
                                use_bias=use_bias)(xr)
    xr = tf.keras.layers.BatchNormalization()(xr)
    xr = tf.keras.layers.ReLU()(xr)
    xr = tf.keras.layers.Conv2D(32, 3, activation='linear', padding='same', kernel_regularizer=l2_weight_decay,
                                use_bias=use_bias)(xr)
    x = tf.keras.layers.Add()([x, xr])

    # fourth block
    x = tf.keras.layers.BatchNormalization()(x)
    xr = tf.keras.layers.ReLU()(x)
    xr = tf.keras.layers.Conv2D(64, 3, strides=(2, 2), activation='linear', padding='same',
                                kernel_regularizer=l2_weight_decay, use_bias=use_bias)(xr)
    xr = tf.keras.layers.BatchNormalization()(xr)
    xr = tf.keras.layers.ReLU()(xr)
    xr = tf.keras.layers.Conv2D(64, 3, activation='linear', padding='same', kernel_regularizer=l2_weight_decay,
                                use_bias=use_bias)(xr)
    x = tf.keras.layers.MaxPooling2D((2, 2), padding='same')(x)
    x = tf.keras.layers.Conv2D(kernel_size=1, filters=64, strides=1, padding="same",
                               kernel_regularizer=l2_weight_decay, use_bias=use_bias)(x)
    x = tf.keras.layers.Add()([x, xr])
    x = tf.keras.layers.BatchNormalization()(x)
    xr = tf.keras.layers.ReLU()(x)
    xr = tf.keras.layers.Conv2D(64, 3, activation='linear', padding='same', kernel_regularizer=l2_weight_decay,
                                use_bias=use_bias)(xr)
    xr = tf.keras.layers.BatchNormalization()(xr)
    xr = tf.keras.layers.ReLU()(xr)
    xr = tf.keras.layers.Conv2D(64, 3, activation='linear', padding='same', kernel_regularizer=l2_weight_decay,
                                use_bias=use_bias)(xr)
    x = tf.keras.layers.Add()([x, xr])

    # fifth block
    x = tf.keras.layers.BatchNormalization()(x)
    xr = tf.keras.layers.ReLU()(x)
    xr = tf.keras.layers.Conv2D(128, 3, strides=(2, 2), activation='linear', padding='same',
                                kernel_regularizer=l2_weight_decay, use_bias=use_bias)(xr)
    xr = tf.keras.layers.BatchNormalization()(xr)
    xr = tf.keras.layers.ReLU()(xr)
    xr = tf.keras.layers.Conv2D(128, 3, activation='linear', padding='same', kernel_regularizer=l2_weight_decay,
                                use_bias=use_bias)(xr)
    x = tf.keras.layers.MaxPooling2D((2, 2), padding='same')(x)
    x = tf.keras.layers.Conv2D(kernel_size=1, filters=128, strides=1, padding="same",
                               kernel_regularizer=l2_weight_decay, use_bias=use_bias)(x)
    x = tf.keras.layers.Add()([x, xr])
    x = tf.keras.layers.BatchNormalization()(x)
    xr = tf.keras.layers.ReLU()(x)
    xr = tf.keras.layers.Conv2D(128, 3, activation='linear', padding='same', kernel_regularizer=l2_weight_decay,
                                use_bias=use_bias)(xr)
    xr = tf.keras.layers.BatchNormalization()(xr)
    xr = tf.keras.layers.ReLU()(xr)
    xr = tf.keras.layers.Conv2D(128, 3, activation='linear', padding='same', kernel_regularizer=l2_weight_decay,
                                use_bias=use_bias)(xr)
    x = tf.keras.layers.Add()([x, xr])

    x = tf.keras.layers.MaxPooling2D((18, 1), padding='same')(x)
    x = tf.keras.layers.Flatten(name='flat')(x)
    x = tf.keras.layers.BatchNormalization()(x)
    emb_mel = tf.keras.layers.Dense(256, kernel_regularizer=l2_weight_decay, name='emb_mel', use_bias=use_bias)(x)

    emb_mel_ssl, emb_fft_ssl, y_ssl = AugLayer(prob=0.5)([emb_mel,emb_fft,y_mix])
    # prepare output
    x = tf.keras.layers.Concatenate(axis=-1)([emb_fft, emb_mel])
    x_ssl = tf.keras.layers.Concatenate(axis=-1)([emb_fft_ssl, emb_mel_ssl])

    output_ssl = AdaProj(n_classes=num_classes*3, n_subclusters=n_subclusters, trainable=False)([x_ssl, y_ssl, label_input])  # compare with trainable equals True
    output = AdaProj(n_classes=num_classes, n_subclusters=n_subclusters, trainable=False)([x, y_mix, label_input])

    loss_output = tf.keras.layers.Lambda(lambda x: tf.stack(x, axis=-1))([output, y_mix])
    loss_output_ssl = tf.keras.layers.Lambda(lambda x: tf.stack(x, axis=-1))([output_ssl, y_ssl])

    return data_input, label_input, loss_output, loss_output_ssl


########################################################################################################################
# Load data and compute embeddings
########################################################################################################################
target_sr = 16000
max_size = 192000  #288000 or 192000
use_ensemble = True

# load train data
print('Loading train data')
categories_dev = os.listdir("./dev_data")
categories_eval = os.listdir("./eval_data")

if os.path.isfile(str(target_sr) + '_train_raw.npy'):
    train_raw = np.load(str(target_sr) + '_train_raw.npy')
    train_ids = np.load('train_ids.npy')
    train_files = np.load('train_files.npy')
    train_atts = np.load('train_atts.npy')
    train_domains = np.load('train_domains.npy')
else:
    train_raw = []
    train_ids = []
    train_files = []
    train_atts = []
    train_domains = []
    dicts = ['./dev_data/']#['./dev_data/', './eval_data/']
    eps = 1e-12
    for dict in dicts:
        for label, category in enumerate(os.listdir(dict)):
            print(category)
            if category=='RoboticArm':  # fix erroneous filenames
                for count, file in tqdm(enumerate(os.listdir(dict + category + "/train")),
                                    total=len(os.listdir(dict + category + "/train"))):
                    if len(file.split('_'))<9:
                        file_path = dict + category + "/train/" + file
                        new_file_path = dict + category + "/train/" + file.split('weight')[0] + 'weight_' + file.split('weight')[1].split('_')[0] + '_Bckg_' + file.split('Bckg')[1] 
                        os.rename(file_path, new_file_path)
            for count, file in tqdm(enumerate(os.listdir(dict + category + "/train")),
                                    total=len(os.listdir(dict + category + "/train"))):
                if file.endswith('.wav'):
                    file_path = dict + category + "/train/" + file
                    wav, fs = sf.read(file_path)
                    raw = librosa.core.to_mono(wav.transpose()).transpose()
                    raw = adjust_size(raw, max_size)
                    train_raw.append(raw)
                    train_ids.append(category + '_' + file.split('_')[1])
                    train_files.append(file_path)
                    train_domains.append(file.split('_')[2])
                    train_atts.append('_'.join(file.split('.wav')[0].split('_')[6:]))
    # reshape arrays and store
    train_ids = np.array(train_ids)
    train_files = np.array(train_files)
    train_raw = np.expand_dims(np.array(train_raw, dtype=np.float32), axis=-1)
    train_atts = np.array(train_atts)
    train_domains = np.array(train_domains)
    np.save('train_ids.npy', train_ids)
    np.save('train_files.npy', train_files)
    np.save('train_atts.npy', train_atts)
    np.save('train_domains.npy', train_domains)
    np.save(str(target_sr) + '_train_raw.npy', train_raw)

# load evaluation data
print('Loading evaluation data')
if os.path.isfile(str(target_sr) + '_eval_raw.npy'):
    eval_raw = np.load(str(target_sr) + '_eval_raw.npy')
    eval_ids = np.load('eval_ids.npy')
    eval_normal = np.load('eval_normal.npy')
    eval_files = np.load('eval_files.npy')
    eval_atts = np.load('eval_atts.npy')
    eval_domains = np.load('eval_domains.npy')
else:
    eval_raw = []
    eval_ids = []
    eval_normal = []
    eval_files = []
    eval_atts = []
    eval_domains = []
    eps = 1e-12
    for label, category in enumerate(os.listdir("./dev_data/")):
        print(category)
        for count, file in tqdm(enumerate(os.listdir("./dev_data/" + category + "/test")),
                                total=len(os.listdir("./dev_data/" + category + "/test"))):
            if file.endswith('.wav'):
                file_path = "./dev_data/" + category + "/test/" + file
                wav, fs = sf.read(file_path)
                raw = librosa.core.to_mono(wav.transpose()).transpose()
                raw = adjust_size(raw, max_size) #288000 or 192000
                eval_raw.append(raw)
                eval_ids.append(category + '_' + file.split('_')[1])
                eval_normal.append(file.split('_test_')[1].split('_')[0] == 'normal')
                eval_files.append(file_path)
                eval_domains.append(file.split('_')[2])
                eval_atts.append('_'.join(file.split('.wav')[0].split('_')[6:]))
    # reshape arrays and store
    eval_ids = np.array(eval_ids)
    eval_normal = np.array(eval_normal)
    eval_files = np.array(eval_files)
    eval_atts = np.array(eval_atts)
    eval_domains = np.array(eval_domains)
    eval_raw = np.expand_dims(np.array(eval_raw, dtype=np.float32), axis=-1)
    np.save('eval_ids.npy', eval_ids)
    np.save('eval_normal.npy', eval_normal)
    np.save('eval_files.npy', eval_files)
    np.save('eval_atts.npy', eval_atts)
    np.save('eval_domains.npy', eval_domains)
    np.save(str(target_sr) + '_eval_raw.npy', eval_raw)

# load test data
print('Loading test data')
if os.path.isfile(str(target_sr) + '_test_raw.npy'):
    test_raw = np.load(str(target_sr) + '_test_raw.npy')
    test_ids = np.load('test_ids.npy')
    test_files = np.load('test_files.npy')
else:
    test_raw = []
    test_ids = []
    test_files = []
    eps = 1e-12
    for label, category in enumerate(os.listdir("./eval_data/")):
        print(category)
        for count, file in tqdm(enumerate(os.listdir("./eval_data/" + category + "/test")),
                                total=len(os.listdir("./eval_data/" + category + "/test"))):
            if file.endswith('.wav'):
                file_path = "./eval_data/" + category + "/test/" + file
                wav, fs = sf.read(file_path)
                raw = librosa.core.to_mono(wav.transpose()).transpose()
                raw = adjust_size(raw, max_size) #288000 or 192000
                test_raw.append(raw)
                test_ids.append(category + '_' + file.split('_')[1])
                test_files.append(file_path)
    # reshape arrays and store
    test_ids = np.array(test_ids)
    test_files = np.array(test_files)
    test_raw = np.expand_dims(np.array(test_raw, dtype=np.float32), axis=-1)
    np.save('test_ids.npy', test_ids)
    np.save('test_files.npy', test_files)
    np.save(str(target_sr) + '_test_raw.npy', test_raw)


# encode ids as labels
le_4train = LabelEncoder()

source_train = np.array([file.split('_')[3] == 'source' for file in train_files.tolist()])
source_eval = np.array([file.split('_')[3] == 'source' for file in eval_files.tolist()])
train_ids_4train = np.array(['###'.join([train_ids[k], train_atts[k], str(source_train[k])]) for k in np.arange(train_ids.shape[0])])
eval_ids_4train = np.array(['###'.join([eval_ids[k], eval_atts[k], str(source_eval[k])]) for k in np.arange(eval_ids.shape[0])])
le_4train.fit(np.concatenate([train_ids_4train, eval_ids_4train], axis=0))
num_classes_4train = len(np.unique(np.concatenate([train_ids_4train, eval_ids_4train], axis=0)))
train_labels_4train = le_4train.transform(train_ids_4train)
eval_labels_4train = le_4train.transform(eval_ids_4train)

le = LabelEncoder().fit(np.concatenate([train_ids, eval_ids, test_ids], axis=0))
train_labels = le.transform(train_ids)
eval_labels = le.transform(eval_ids)
test_labels = le.transform(test_ids)
all_labels = np.unique(np.concatenate([train_labels, eval_labels, test_labels], axis=0))
num_classes = len(all_labels)

# define sample weights
sample_weights = np.ones(train_ids_4train.shape)
for id_4train in np.unique(train_ids_4train[source_train]):
    section = id_4train.split('###')[0]
    domain = np.bool(id_4train.split('###')[-1])
    sample_weights[train_ids_4train == id_4train] = np.sum((train_ids_4train != id_4train) * source_train)

# normalize weights for each machine type
for lab in np.unique(train_labels):
    sample_weights[(train_labels==lab)*source_train] = sample_weights[(train_labels==lab)*source_train]/np.sum(sample_weights[(train_labels==lab)*source_train])

# re-scale weights
sample_weights /= np.mean(sample_weights[source_train])

# distinguish between normal and anomalous samples on development set
unknown_raw = eval_raw[~eval_normal]
unknown_labels = eval_labels[~eval_normal]
unknown_labels_4train = eval_labels_4train[~eval_normal]
unknown_files = eval_files[~eval_normal]
unknown_ids = eval_ids[~eval_normal]
unknown_domains = eval_domains[~eval_normal]
source_unknown = source_eval[~eval_normal]
eval_raw = eval_raw[eval_normal]
eval_labels = eval_labels[eval_normal]
eval_labels_4train = eval_labels_4train[eval_normal]
eval_files = eval_files[eval_normal]
eval_ids = eval_ids[eval_normal]
eval_domains = eval_domains[eval_normal]
source_eval = source_eval[eval_normal]

# training parameters
batch_size = 32
batch_size_test = 32
epochs = 10
aeons = 1
alpha = 1
n_subclusters = 32
ensemble_size = 10

final_results_dev = np.zeros((ensemble_size, 6))
final_results_eval = np.zeros((ensemble_size, 6))

pred_eval = np.zeros((eval_raw.shape[0], num_classes, 2))
pred_unknown = np.zeros((unknown_raw.shape[0], num_classes, 2))
pred_test = np.zeros((test_raw.shape[0], num_classes, 2))
pred_train = np.zeros((train_labels.shape[0], num_classes, 2))

for k_ensemble in np.arange(ensemble_size):
    # prepare scores and domain info
    y_train_cat = tf.keras.utils.to_categorical(train_labels, num_classes=num_classes)
    y_eval_cat = tf.keras.utils.to_categorical(eval_labels, num_classes=num_classes)
    y_unknown_cat = tf.keras.utils.to_categorical(unknown_labels, num_classes=num_classes)

    y_train_cat_4train = tf.keras.utils.to_categorical(train_labels_4train, num_classes=num_classes_4train)
    y_eval_cat_4train = tf.keras.utils.to_categorical(eval_labels_4train, num_classes=num_classes_4train)
    y_unknown_cat_4train = tf.keras.utils.to_categorical(unknown_labels_4train, num_classes=num_classes_4train)

    # compile model
    data_input, label_input, loss_output, loss_output_ssl = model_emb_cnn(num_classes=num_classes_4train,
                                                             raw_dim=eval_raw.shape[1], n_subclusters=n_subclusters, use_bias=False)
    model = tf.keras.Model(inputs=[data_input, label_input], outputs=[loss_output, loss_output_ssl])
    model.compile(loss=[mixupLoss, mixupLoss], optimizer=tf.keras.optimizers.Adam() ,loss_weights=[1,1])
    print(model.summary())
    for k in np.arange(aeons):
        print('ensemble iteration: ' + str(k_ensemble+1))
        print('aeon: ' + str(k+1))
        # fit model
        weight_path = 'wts_' + str(k+1) + 'k_' + str(target_sr) + '_' + str(k_ensemble+1) + '_final_only-dev.h5'
        if not os.path.isfile(weight_path):
            model.fit(
                [train_raw[source_train], y_train_cat_4train[source_train]],
                [y_train_cat_4train[source_train], y_train_cat_4train[source_train]],
                verbose=1,
                batch_size=batch_size, epochs=epochs,
                validation_data=(
                [eval_raw, y_eval_cat_4train], [y_eval_cat_4train, y_eval_cat_4train]),
                sample_weight=sample_weights[source_train]
                )
            model.save(weight_path)
            model.save(weight_path)
        else:
            model = tf.keras.models.load_model(weight_path,
                                               custom_objects={'MixupLayer': MixupLayer, 'mixupLoss': mixupLoss,
                                                               'SCAdaCos': SCAdaCos, 'AdaProj': AdaProj,
                                                               'MagnitudeSpectrogram': MagnitudeSpectrogram, 'AugLayer': AugLayer})

        # extract embeddings
        emb_model = tf.keras.Model(model.input, model.layers[-6].output)
        eval_embs = emb_model.predict([eval_raw, np.zeros((eval_raw.shape[0], num_classes_4train))], batch_size=batch_size)
        train_embs = emb_model.predict([train_raw, np.zeros((train_raw.shape[0], num_classes_4train))], batch_size=batch_size)
        unknown_embs = emb_model.predict([unknown_raw, np.zeros((unknown_raw.shape[0], num_classes_4train))], batch_size=batch_size)
        test_embs = emb_model.predict([test_raw, np.zeros((test_raw.shape[0], num_classes_4train))], batch_size=batch_size)

        # length normalization
        x_train_ln = length_norm(train_embs)
        x_eval_ln = length_norm(eval_embs)
        x_test_ln = length_norm(test_embs)
        x_unknown_ln = length_norm(unknown_embs)

        for j, lab in tqdm(enumerate(all_labels)):
            if np.sum(train_labels == lab)>0:
                cat = le.inverse_transform([lab])[0]
                kmeans = KMeans(n_clusters=n_subclusters, random_state=0).fit(x_train_ln[source_train*(train_labels == lab)])
                means_source_ln = kmeans.cluster_centers_
                means_target_ln = x_train_ln[~source_train * (train_labels == lab)]

                # compute cosine distances
                if use_ensemble:
                    pred_train[train_labels == lab, j, 0] += np.min(2*(1-np.dot(x_train_ln[train_labels == lab], means_target_ln.transpose())), axis=-1)
                    pred_train[train_labels == lab, j, 1] += np.min(2*(1-np.dot(x_train_ln[train_labels == lab], means_source_ln.transpose())), axis=-1)
                else:
                    pred_train[train_labels == lab, j, 0] = np.min(2*(1-np.dot(x_train_ln[train_labels == lab], means_target_ln.transpose())), axis=-1)
                    pred_train[train_labels == lab, j, 1] = np.min(2*(1-np.dot(x_train_ln[train_labels == lab], means_source_ln.transpose())), axis=-1)
                if np.sum(eval_labels == lab) > 0:
                    if use_ensemble:
                        pred_eval[eval_labels == lab, j,0] += np.min(2*(1-np.dot(x_eval_ln[eval_labels == lab], means_target_ln.transpose())),axis=-1)
                        pred_eval[eval_labels == lab, j,1] += np.min(2*(1-np.dot(x_eval_ln[eval_labels == lab], means_source_ln.transpose())), axis=-1)
                        pred_unknown[unknown_labels == lab, j,0] += np.min(2*(1-np.dot(x_unknown_ln[unknown_labels == lab], means_target_ln.transpose())), axis=-1)
                        pred_unknown[unknown_labels == lab, j,1] += np.min(2*(1-np.dot(x_unknown_ln[unknown_labels == lab], means_source_ln.transpose())), axis=-1)
                    else:
                        pred_eval[eval_labels == lab, j,0] = np.min(2*(1-np.dot(x_eval_ln[eval_labels == lab], means_target_ln.transpose())),axis=-1)
                        pred_eval[eval_labels == lab, j,1] = np.min(2*(1-np.dot(x_eval_ln[eval_labels == lab], means_source_ln.transpose())), axis=-1)
                        pred_unknown[unknown_labels == lab, j,0] = np.min(2*(1-np.dot(x_unknown_ln[unknown_labels == lab], means_target_ln.transpose())), axis=-1)
                        pred_unknown[unknown_labels == lab, j,1] = np.min(2*(1-np.dot(x_unknown_ln[unknown_labels == lab], means_source_ln.transpose())), axis=-1)
                if np.sum(test_labels == lab) > 0:
                    if use_ensemble:
                        pred_test[test_labels == lab, j, 0] += np.min(2*(1-np.dot(x_test_ln[test_labels == lab], means_target_ln.transpose())), axis=-1)
                        pred_test[test_labels == lab, j, 1] += np.min(2*(1-np.dot(x_test_ln[test_labels == lab], means_source_ln.transpose())), axis=-1)
                    else:
                        pred_test[test_labels == lab, j, 0] = np.min(2*(1-np.dot(x_test_ln[test_labels == lab], means_target_ln.transpose())), axis=-1)
                        pred_test[test_labels == lab, j, 1] = np.min(2*(1-np.dot(x_test_ln[test_labels == lab], means_source_ln.transpose())), axis=-1)

        print('#######################################################################################################')
        print('DEVELOPMENT SET')
        print('#######################################################################################################')
        aucs = []
        p_aucs = []
        aucs_source = []
        p_aucs_source = []
        aucs_target = []
        p_aucs_target = []
        for j, cat in enumerate(np.unique(eval_ids)):
            y_pred = np.concatenate([np.min(pred_eval[eval_labels == le.transform([cat]), le.transform([cat])], axis=-1),
                                     np.min(pred_unknown[unknown_labels == le.transform([cat]), le.transform([cat])], axis=-1)],
                                     axis=0)
            y_true = np.concatenate([np.zeros(np.sum(eval_labels == le.transform([cat]))),
                                     np.ones(np.sum(unknown_labels == le.transform([cat])))], axis=0)
            auc = roc_auc_score(y_true, y_pred)
            aucs.append(auc)
            p_auc = roc_auc_score(y_true, y_pred, max_fpr=0.1)
            p_aucs.append(p_auc)
            print('AUC for category ' + str(cat) + ': ' + str(np.round(auc * 100, 1)))
            print('pAUC for category ' + str(cat) + ': ' + str(np.round(p_auc * 100, 1)))

            source_all = np.concatenate([source_eval[eval_labels == le.transform([cat])],
                                         source_unknown[unknown_labels == le.transform([cat])]], axis=0)
            auc = roc_auc_score(y_true[source_all], y_pred[source_all])
            p_auc = roc_auc_score(y_true[source_all], y_pred[source_all], max_fpr=0.1)
            aucs_source.append(auc)
            p_aucs_source.append(p_auc)
            print('AUC for source domain of category ' + str(cat) + ': ' + str(np.round(auc * 100, 1)))
            print('pAUC for source domain of category ' + str(cat) + ': ' + str(np.round(p_auc * 100, 1)))
            auc = roc_auc_score(y_true[~source_all], y_pred[~source_all])
            p_auc = roc_auc_score(y_true[~source_all], y_pred[~source_all], max_fpr=0.1)
            aucs_target.append(auc)
            p_aucs_target.append(p_auc)
            print('AUC for target domain of category ' + str(cat) + ': ' + str(np.round(auc * 100, 1)))
            print('pAUC for target domain of category ' + str(cat) + ': ' + str(np.round(p_auc * 100, 1)))
        print('####################')
        aucs = np.array(aucs)
        p_aucs = np.array(p_aucs)
        for cat in categories_dev:
            mean_auc = hmean(aucs[np.array([eval_id.split('_')[0] for eval_id in np.unique(eval_ids)]) == cat])
            print('mean AUC for category ' + str(cat) + ': ' + str(np.round(mean_auc * 100, 1)))
            mean_p_auc = hmean(p_aucs[np.array([eval_id.split('_')[0] for eval_id in np.unique(eval_ids)]) == cat])
            print('mean pAUC for category ' + str(cat) + ': ' + str(np.round(mean_p_auc * 100, 1)))
        print('####################')
        for cat in categories_dev:
            mean_auc = hmean(aucs[np.array([eval_id.split('_')[0] for eval_id in np.unique(eval_ids)]) == cat])
            mean_p_auc = hmean(p_aucs[np.array([eval_id.split('_')[0] for eval_id in np.unique(eval_ids)]) == cat])
            print('mean of AUC and pAUC for category ' + str(cat) + ': ' + str(np.round((mean_p_auc + mean_auc) * 50, 1)))
        print('####################')
        mean_auc_source = hmean(aucs_source)
        print('mean AUC for source domain: ' + str(np.round(mean_auc_source * 100, 1)))
        mean_p_auc_source = hmean(p_aucs_source)
        print('mean pAUC for source domain: ' + str(np.round(mean_p_auc_source * 100, 1)))
        mean_auc_target = hmean(aucs_target)
        print('mean AUC for target domain: ' + str(np.round(mean_auc_target * 100, 1)))
        mean_p_auc_target = hmean(p_aucs_target)
        print('mean pAUC for target domain: ' + str(np.round(mean_p_auc_target * 100, 1)))
        mean_auc = hmean(aucs)
        print('mean AUC: ' + str(np.round(mean_auc * 100, 1)))
        mean_p_auc = hmean(p_aucs)
        print('mean pAUC: ' + str(np.round(mean_p_auc * 100, 1)))
        final_results_dev[k_ensemble] = np.array([mean_auc_source, mean_p_auc_source, mean_auc_target, mean_p_auc_target, mean_auc, mean_p_auc])
"""
        # print results for eval set
        print('#######################################################################################################')
        print('EVALUATION SET')
        print('#######################################################################################################')
        aucs = []
        p_aucs = []
        aucs_source = []
        p_aucs_source = []
        aucs_target = []
        p_aucs_target = []
        for j, cat in enumerate(np.unique(test_ids)):
            y_pred = np.min(pred_test[test_labels == le.transform([cat]), le.transform([cat])], axis=-1)
            y_true = np.array(pd.read_csv(
                './dcase2023_task2_evaluator-main/ground_truth_data/ground_truth_' + cat.split('_')[0] + '_section_' + cat.split('_')[1] + '_test.csv', header=None).iloc[:, 1] == 1)
            auc = roc_auc_score(y_true, y_pred)
            aucs.append(auc)
            p_auc = roc_auc_score(y_true, y_pred, max_fpr=0.1)
            p_aucs.append(p_auc)
            print('AUC for category ' + str(cat) + ': ' + str(np.round(auc * 100, 1)))
            print('pAUC for category ' + str(cat) + ': ' + str(np.round(p_auc * 100, 1)))
            source_all = np.array(pd.read_csv(
                './dcase2023_task2_evaluator-main/ground_truth_domain/ground_truth_' + cat.split('_')[0] + '_section_' + cat.split('_')[1] + '_test.csv', header=None).iloc[:, 1] == 0)
            auc = roc_auc_score(y_true[source_all], y_pred[source_all])
            p_auc = roc_auc_score(y_true[source_all], y_pred[source_all], max_fpr=0.1)
            aucs_source.append(auc)
            p_aucs_source.append(p_auc)
            print('AUC for source domain of category ' + str(cat) + ': ' + str(np.round(auc * 100, 1)))
            print('pAUC for source domain of category ' + str(cat) + ': ' + str(np.round(p_auc * 100, 1)))
            auc = roc_auc_score(y_true[~source_all], y_pred[~source_all])
            p_auc = roc_auc_score(y_true[~source_all], y_pred[~source_all], max_fpr=0.1)
            aucs_target.append(auc)
            p_aucs_target.append(p_auc)
            print('AUC for target domain of category ' + str(cat) + ': ' + str(np.round(auc * 100, 1)))
            print('pAUC for target domain of category ' + str(cat) + ': ' + str(np.round(p_auc * 100, 1)))
        print('####################')
        aucs = np.array(aucs)
        p_aucs = np.array(p_aucs)
        for cat in categories_eval:
            mean_auc = hmean(aucs[np.array([test_id.split('_')[0] for test_id in np.unique(test_ids)]) == cat])
            print('mean AUC for category ' + str(cat) + ': ' + str(np.round(mean_auc * 100, 1)))
            mean_p_auc = hmean(p_aucs[np.array([test_id.split('_')[0] for test_id in np.unique(test_ids)]) == cat])
            print('mean pAUC for category ' + str(cat) + ': ' + str(np.round(mean_p_auc * 100, 1)))
        print('####################')
        for cat in categories_eval:
            mean_auc = hmean(aucs[np.array([test_id.split('_')[0] for test_id in np.unique(test_ids)]) == cat])
            mean_p_auc = hmean(p_aucs[np.array([test_id.split('_')[0] for test_id in np.unique(test_ids)]) == cat])
            print('mean of AUC and pAUC for category ' + str(cat) + ': ' + str(np.round((mean_p_auc + mean_auc) * 50, 1)))
        print('####################')
        mean_auc_source = hmean(aucs_source)
        print('mean AUC for source domain: ' + str(np.round(mean_auc_source * 100, 1)))
        mean_p_auc_source = hmean(p_aucs_source)
        print('mean pAUC for source domain: ' + str(np.round(mean_p_auc_source * 100, 1)))
        mean_auc_target = hmean(aucs_target)
        print('mean AUC for target domain: ' + str(np.round(mean_auc_target * 100, 1)))
        mean_p_auc_target = hmean(p_aucs_target)
        print('mean pAUC for target domain: ' + str(np.round(mean_p_auc_target * 100, 1)))
        mean_auc = hmean(aucs)
        print('mean AUC: ' + str(np.round(mean_auc * 100, 1)))
        mean_p_auc = hmean(p_aucs)
        print('mean pAUC: ' + str(np.round(mean_p_auc * 100, 1)))
        final_results_eval[k_ensemble] = np.array([mean_auc_source, mean_p_auc_source, mean_auc_target, mean_p_auc_target, mean_auc, mean_p_auc])
"""

# create challenge submission files
print('creating submission files')
sub_path = './teams/submission/team_fkie'
if not os.path.exists(sub_path):
    os.makedirs(sub_path)
for j, cat in enumerate(np.unique(test_ids)):
    # anomaly scores
    file_idx = test_labels == le.transform([cat])
    results_an = pd.DataFrame()
    results_an['output1'], results_an['output2'] = [[f.split('/')[-1] for f in test_files[file_idx]],
                                                    [str(s) for s in np.min(pred_test[file_idx, le.transform([cat])], axis=-1)]]
    results_an.to_csv(sub_path + '/anomaly_score_' + cat.split('_')[0] + '_section_' + cat.split('_')[-1] + '_test.csv',
                      encoding='utf-8', index=False, header=False)

    # decision results
    train_scores = np.min(pred_train[train_labels == le.transform([cat]), le.transform([cat])], axis=-1)
    threshold = np.percentile(train_scores, q=90)
    decisions = np.min(pred_test[file_idx, le.transform([cat])], axis=-1) > threshold
    results_dec = pd.DataFrame()
    results_dec['output1'], results_dec['output2'] = [[f.split('/')[-1] for f in test_files[file_idx]],
                                                      [str(int(s)) for s in decisions]]
    results_dec.to_csv(sub_path + '/decision_result_' + cat.split('_')[0] + '_section_' + cat.split('_')[-1] + '_test.csv',
                       encoding='utf-8', index=False, header=False)

print('####################')
print('####################')
print('####################')
if use_ensemble:
    print('final results for development set')
    print(np.round(final_results_dev[-1]*100, 1))
    #print('final results for evaluation set')
    #print(np.round(final_results_eval[-1]*100, 1))
else:
    print('final results for development set')
    print(np.round(np.mean(final_results_dev*100, axis=0), 1))
    print(np.round(np.std(final_results_dev*100, axis=0), 1))
    #print('final results for evaluation set')
    #print(np.round(np.mean(final_results_eval*100, axis=0), 1))
    #print(np.round(np.std(final_results_eval*100, axis=0), 1))

print('####################')
print('>>>> finished! <<<<<')
print('####################')