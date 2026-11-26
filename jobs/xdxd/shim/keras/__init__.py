"""Minimal Keras-1.1 API shim on PyTorch, exactly the subset XD_XD's SpaceNet-2 code uses
(functional U-Net of Convolution2D / MaxPooling2D / UpSampling2D / merge(concat), Adam / SGD,
binary_crossentropy, fit / fit_generator / predict / predict_generator, ModelCheckpoint /
EarlyStopping / History, save_weights / load_weights). Channels-first ('th') like the original.
Keras-1 semantics kept: glorot_uniform conv init, zero bias, zero-based epoch numbers in
checkpoint names, batch-averaged train metrics, val metrics averaged over batches of 32,
EarlyStopping 'wait' logic, fit_generator epochs defined by samples_per_epoch."""
