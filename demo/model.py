import numpy as np
import tensorflow as tf
import os
import matplotlib.pyplot as plt
import imageio
import time
import pickle
from tqdm import tqdm
from PIL import Image
from threading import Lock




def save_obj(obj, name):
    """:param obj: object to save
       :param: name: name of the object"""
    with open(name + '.pkl', 'wb') as f:
        pickle.dump(obj, f, pickle.HIGHEST_PROTOCOL)

def load_obj(name):
    """ :param: name: name of the object to load"""
    with open(name + '.pkl', 'rb') as f:
        return pickle.load(f)

lock = Lock()


def get(name):
    return tf.get_default_graph().get_tensor_by_name('import/' + name + ':0')


def tensorflow_session():
    # Init session and params
    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    # Pin GPU to local rank (one GPU per process)
    config.gpu_options.visible_device_list = str(0)
    sess = tf.Session(config=config)
    return sess


optimized = True
if optimized:
    # Optimized model. Twice as fast as
    # 1. we freeze conditional network (label is always 0)
    # 2. we use fused kernels
    import blocksparse
    graph_path = 'graph_optimized.pb'
    inputs = {
        'dec_eps_0': 'dec_eps_0',
        'dec_eps_1': 'dec_eps_1',
        'dec_eps_2': 'dec_eps_2',
        'dec_eps_3': 'dec_eps_3',
        'dec_eps_4': 'dec_eps_4',
        'dec_eps_5': 'dec_eps_5',
        'enc_x': 'input/enc_x',
    }
    outputs = {
        'dec_x': 'model_3/Cast_1',
        'enc_eps_0': 'model_2/pool0/truediv_1',
        'enc_eps_1': 'model_2/pool1/truediv_1',
        'enc_eps_2': 'model_2/pool2/truediv_1',
        'enc_eps_3': 'model_2/pool3/truediv_1',
        'enc_eps_4': 'model_2/pool4/truediv_1',
        'enc_eps_5': 'model_2/truediv_4'
    }

    def update_feed(feed_dict, bs):
        return feed_dict
else:
    graph_path = 'graph_unoptimized.pb'
    inputs = {
        'dec_eps_0': 'Placeholder',
        'dec_eps_1': 'Placeholder_1',
        'dec_eps_2': 'Placeholder_2',
        'dec_eps_3': 'Placeholder_3',
        'dec_eps_4': 'Placeholder_4',
        'dec_eps_5': 'Placeholder_5',
        'enc_x': 'input/image',
        'enc_x_d': 'input/downsampled_image',
        'enc_y': 'input/label'
    }
    outputs = {
        'dec_x': 'model_1/Cast_1',
        'enc_eps_0': 'model/pool0/truediv_1',
        'enc_eps_1': 'model/pool1/truediv_1',
        'enc_eps_2': 'model/pool2/truediv_1',
        'enc_eps_3': 'model/pool3/truediv_1',
        'enc_eps_4': 'model/pool4/truediv_1',
        'enc_eps_5': 'model/truediv_4'
    }

    def update_feed(feed_dict, bs):
        x_d = 128 * np.ones([bs, 128, 128, 3], dtype=np.uint8)
        y = np.zeros([bs], dtype=np.int32)
        feed_dict[enc_x_d] = x_d
        feed_dict[enc_y] = y
        return feed_dict

with tf.gfile.GFile(graph_path, 'rb') as f:
    graph_def_optimized = tf.GraphDef()
    graph_def_optimized.ParseFromString(f.read())

sess = tensorflow_session()
tf.import_graph_def(graph_def_optimized)

print("Loaded model")

n_eps = 6

# Encoder
enc_x = get(inputs['enc_x'])
enc_eps = [get(outputs['enc_eps_' + str(i)]) for i in range(n_eps)]
if not optimized:
    enc_x_d = get(inputs['enc_x_d'])
    enc_y = get(inputs['enc_y'])

# Decoder
dec_x = get(outputs['dec_x'])
dec_eps = [get(inputs['dec_eps_' + str(i)]) for i in range(n_eps)]

eps_shapes = [(128, 128, 6), (64, 64, 12), (32, 32, 24),
              (16, 16, 48), (8, 8, 96), (4, 4, 384)]
eps_sizes = [np.prod(e) for e in eps_shapes]
eps_size = 256 * 256 * 3
z_manipulate = np.load('z_manipulate.npy')

_TAGS = "5_o_Clock_Shadow Arched_Eyebrows Attractive Bags_Under_Eyes Bald Bangs Big_Lips Big_Nose Black_Hair Blond_Hair Blurry Brown_Hair Bushy_Eyebrows Chubby Double_Chin Eyeglasses Goatee Gray_Hair Heavy_Makeup High_Cheekbones Male Mouth_Slightly_Open Mustache Narrow_Eyes No_Beard Oval_Face Pale_Skin Pointy_Nose Receding_Hairline Rosy_Cheeks Sideburns Smiling Straight_Hair Wavy_Hair Wearing_Earrings Wearing_Hat Wearing_Lipstick Wearing_Necklace Wearing_Necktie Young"
_TAGS = _TAGS.split()

flip_tags = ['No_Beard', 'Young']
for tag in flip_tags:
    i = _TAGS.index(tag)
    z_manipulate[i] = -z_manipulate[i]

scale_tags = ['Narrow_Eyes']
for tag in scale_tags:
    i = _TAGS.index(tag)
    z_manipulate[i] = 1.2*z_manipulate[i]

z_sq_norms = np.sum(z_manipulate**2, axis=-1, keepdims=True)
z_proj = (z_manipulate / z_sq_norms).T


def run(sess, fetches, feed_dict):
    with lock:
        # Locked tensorflow so average server response time to user is lower
        result = sess.run(fetches, feed_dict)
    return result


def flatten_eps(eps):
    # [BS, eps_size]
    return np.concatenate([np.reshape(e, (e.shape[0], -1)) for e in eps], axis=-1)


def unflatten_eps(feps):
    index = 0
    eps = []
    bs = feps.shape[0]  # feps.size // eps_size
    for shape in eps_shapes:
        eps.append(np.reshape(
            feps[:, index: index+np.prod(shape)], (bs, *shape)))
        index += np.prod(shape)
    return eps


def encode(img):
    if len(img.shape) == 3:
        img = np.expand_dims(img, 0)
    bs = img.shape[0]
    assert img.shape[1:] == (256, 256, 3)
    feed_dict = {enc_x: img}

    update_feed(feed_dict, bs)  # For unoptimized model
    return flatten_eps(run(sess, enc_eps, feed_dict))


def decode(feps):
    if len(feps.shape) == 1:
        feps = np.expand_dims(feps, 0)
    bs = feps.shape[0]
    # assert len(eps) == n_eps
    # for i in range(n_eps):
    #     shape = (BATCH_SIZE, 128 // (2 ** i), 128 // (2 ** i), 6 * (2 ** i) * (2 ** (i == (n_eps - 1))))
    #     assert eps[i].shape == shape
    eps = unflatten_eps(feps)

    feed_dict = {}
    for i in range(n_eps):
        feed_dict[dec_eps[i]] = eps[i]

    update_feed(feed_dict, bs)  # For unoptimized model
    return run(sess, dec_x, feed_dict)


def project(z):
    return np.dot(z, z_proj)


def _manipulate(z, dz, alpha):
    z = z + alpha * dz
    return decode(z), z


def _manipulate_range(z, dz, points, scale):
    z_range = np.concatenate(
        [z + scale*(pt/(points - 1)) * dz for pt in range(0, points)], axis=0)
    return decode(z_range), z_range


# alpha from [0,1]
def mix(z1, z2, alpha):
    dz = (z2 - z1)
    return _manipulate(z1, dz, alpha)


def mix_range(z1, z2, points=5):
    dz = (z2 - z1)
    return _manipulate_range(z1, dz, points, 1.)


# alpha goes from [-1,1]
def manipulate(z, typ, alpha):
    dz = z_manipulate[typ]
    return _manipulate(z, dz, alpha)


def manipulate_all(z, typs, alphas):
    dz = 0.0
    for i in range(len(typs)):
        dz += alphas[i] * z_manipulate[typs[i]]
    return _manipulate(z, dz, 1.0)


def manipulate_range(z, typ, points=5, scale=1):
    dz = z_manipulate[typ]
    return _manipulate_range(z - dz, 2*dz, points, scale)


def random(bs=1, eps_std=0.7):
    feps = np.random.normal(scale=eps_std, size=[bs, eps_size])
    return decode(feps), feps


def test():
    # img = Image.open('test/img.png')
    # img = np.reshape(np.array(img), [1, 256, 256, 3])

    # Encoding speed
    # eps = encode(img)
    # t = time.time()
    # for _ in tqdm(range(10)):
    #     eps = encode(img)
    # print("Encoding latency {} sec/img".format((time.time() - t) / (1 * 10)))

    # Decoding speed
    # dec = decode(eps)
    # t = time.time()
    # # for _ in tqdm(range(10)):
    # #     dec = decode(eps)
    # # print("Decoding latency {} sec/img".format((time.time() - t) / (1 * 10)))
    # img = Image.fromarray(dec[0])
    # img.save('test/dec.png')
    #
    # # Manipulation
    # dec, _ = manipulate(eps, _TAGS.index('Smiling'), 0.66)
    # img = Image.fromarray(dec[0])
    # img.save('test/smile.png')


    # warm start
    for i in range(1):
        _img, _z = random(1)
        print(f'shape of z: {_z.shape}')
        img = Image.fromarray(_img[0])
        img.save('test/radnom_img_'+str(i)+'.png')
        # _z = encode(_img)
        print(f'generated {i} images')


def get_table_of_latents():
    img_folder = os.path.join('/home/nird/glow/demo/', 'Female_Combinations_normalized')
    latents_list = []
    file_name_to_save = os.path.join('/home/nird/glow/demo/', 'Female_head_combinations_normalized_latents')

    for img in os.listdir(img_folder):
        img = os.path.join(img_folder, img)
        img = Image.open(img)
        img = np.array(img)
        curr_latent = encode(img)
        latents_list.append(curr_latent)

    latents_list = np.array(latents_list)
    latents_list = np.squeeze(latents_list, axis=1)
    save_obj(latents_list, file_name_to_save)
    print("object saved")


def normalize_and_get_table_of_latents(img_folder, file_name_to_save):

    images_list = []
    images_names = []
    latents_list = []

    for img in sorted(os.listdir(img_folder)):
        images_names.append(img)
        img = os.path.join(img_folder, img)
        img = Image.open(img)
        img = np.array(img)
        images_list.append(img)

    images_np_array = np.array(images_list)

    # max_value = images_np_array.max()
    # min_value = images_np_array.min()
    for np_image, img_name in zip(images_np_array, images_names):
        # np_image_normalized = (np_image - min_value) / (max_value - min_value)
        curr_latent = encode(np_image)
        latents_list.append(curr_latent)

    latents_list = np.array(latents_list)
    latents_list = np.squeeze(latents_list, axis=1)
    save_obj(latents_list, file_name_to_save)
    print("object saved")


def decode_orig_and_pred(path, orig_reference_file_name_female, orig_reference_file_name_male,orig_other_file_name_female,orig_other_file_name_male, pred_file_name_female, pred_file_name_male, alpha, base_num, other_num):
    orig_path_reference_female = os.path.join(path,orig_reference_file_name_female)
    orig_path_reference_male = os.path.join(path, orig_reference_file_name_male)

    orig_path_other_female = os.path.join(path,orig_other_file_name_female)
    orig_path_othermale = os.path.join(path,orig_other_file_name_male)

    pred_path_female = os.path.join(path, pred_file_name_female)
    pred_path_male = os.path.join(path, pred_file_name_male)

    orig_reference_female = load_obj(orig_path_reference_female)
    orig_reference_male = load_obj(orig_path_reference_male)

    orig_other_female = load_obj(orig_path_other_female)
    orig_other_male = load_obj(orig_path_othermale)

    pred_female = load_obj(pred_path_female)
    pred_male = load_obj(pred_path_male)

    decoded_orig_reference_female = decode(orig_reference_female)
    decoded_orig_reference_male = decode(orig_reference_male)

    decoded_orig_other_female = decode(orig_other_female)
    decoded_orig_other_male = decode(orig_other_male)

    decoded_pred_female = decode(pred_female)
    decoded_pred_male = decode(pred_male)

    img_orig_reference_female = Image.fromarray(decoded_orig_reference_female[0])
    img_orig_reference_male = Image.fromarray(decoded_orig_reference_male[0])

    img_orig_other_female = Image.fromarray(decoded_orig_other_female[0])
    img_orig_other_male = Image.fromarray(decoded_orig_other_male[0])

    img_pred_female = Image.fromarray(decoded_pred_female[0])
    img_pred_male = Image.fromarray(decoded_pred_male[0])

    name_orig_reference_female = 'orig_decoded_reference_female'
    name_orig_reference_male = 'orig_decoded_reference_male'

    name_orig_other_female = 'orig_decoded_other_female'
    name_orig_other_male = 'orig_decoded_other_male'

    name_pred_female = 'pred_decoded_female'
    name_pred_male = 'pred_decoded_male'

    img_orig_reference_female.save(os.path.join(path, name_orig_reference_female + '.png'))
    img_orig_reference_male.save(os.path.join(path, name_orig_reference_male + '.png'))

    img_orig_other_female.save(os.path.join(path, name_orig_other_female+'.png'))
    img_orig_other_male.save(os.path.join(path, name_orig_other_male+'.png'))

    img_pred_female.save(os.path.join(path, name_pred_female+'.png'))
    img_pred_male.save(os.path.join(path, name_pred_male+'.png'))

    print("files saved to disk")

def encode_decode_with_and_without_normalize(img_folder,img):
    img = os.path.join(img_folder, img)
    img = Image.open(img)
    img = np.array(img)
    img_normalized = (img - img.min()) / (img.max() - img.min())
    encoded_orig = encode(img)
    encoded_normalized = encode(img_normalized)
    decoded_orig = decode(encoded_orig)
    decoded_normalized = decode(encoded_normalized)
    img_orig_decoded = Image.fromarray(decoded_orig[0])
    img_normalized_decoded = Image.fromarray(decoded_normalized[0])
    name_img_orig_decoded = 'img_orig_decoded'
    name_img_normalized_decoded = 'img_normalized_decoded'
    img_orig_decoded.save(os.path.join(img_folder, name_img_orig_decoded + '.png'))
    img_normalized_decoded.save(os.path.join(img_folder, name_img_normalized_decoded + '.png'))
    print("saved both files")




if __name__ == '__main__':
    list_of_visible_devices = "2,3"
    os.environ["CUDA_VISIBLE_DEVICES"] = list_of_visible_devices
    # test()
    # get_table_of_latents()
    # img_folder = os.path.join('/home/nird/glow/demo/', 'Female')
    # file_name_to_save = os.path.join('/home/nird/glow/demo/', 'Female_head_combinations_normalized_latents_new')
    # normalize_and_get_table_of_latents(img_folder, file_name_to_save)
    #
    # img_folder = os.path.join('/home/nird/glow/demo/', 'Male')
    # file_name_to_save = os.path.join('/home/nird/glow/demo/', 'Male_head_combinations_normalized_latents_new')
    # normalize_and_get_table_of_latents(img_folder,file_name_to_save)
    experiment_name = 'ref_500_other_900'
    path = os.path.join('/home/nird/glow/demo/experiments/',experiment_name)
    orig_reference_file_name_female = 'reference_latent_female'
    orig_reference_file_name_male = 'reference_latent_male'
    orig_other_file_name_female ='other_latent_female'
    orig_other_file_name_male ='other_latent_male'
    pred_file_name_female = 'other_latent_female_pred'
    pred_file_name_male = 'other_latent_male_pred'
    alpha = '5'
    base_num = '0'
    other_num = '50'
    decode_orig_and_pred(path, orig_reference_file_name_female,orig_other_file_name_male,orig_other_file_name_female, orig_other_file_name_male, pred_file_name_female, pred_file_name_male,alpha,base_num,other_num)
    #
    # file_name = 'Opened_GLOBAL_R10_Smile_Lips_Opened_GLOBAL10_Female.jpg'
    # encode_decode_with_and_without_normalize(path, file_name)

