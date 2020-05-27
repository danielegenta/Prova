"""
    This class implements the main model of iCaRL 
    and all the methods regarding the exemplars

    from delivery: iCaRL is made up of 2 components
    - feature extractor (a convolutional NN) => resnet32 optimized on cifar100
    - classifier => a FC layer OR a non-parametric classifier (NME)

    main ref: https://github.com/donlee90/icarl
"""


import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.autograd import Variable
import numpy as np
from PIL import Image
from torch.utils.data import DataLoader
from torch.backends import cudnn
from torch.autograd import Variable
from Cifar100.resnet import resnet32
from Cifar100.Dataset.cifar100 import CIFAR100
import copy
import gc
from torchvision import transforms


from Cifar100 import utils


# Hyper Parameters
# ...

# feature size: 2048
# n_classes: 10 => 100
class ICaRL(nn.Module):
  def __init__(self, feature_size, n_classes, BATCH_SIZE, WEIGHT_DECAY, LR, GAMMA, NUM_EPOCHS, DEVICE,MILESTONES,MOMENTUM,K, reverse_index = None):
    super(ICaRL, self).__init__()
    self.feature_extractor = resnet32()
    self.feature_extractor.fc = nn.Linear(self.feature_extractor.fc.in_features,feature_size)
    self.bn = nn.BatchNorm1d(feature_size, momentum=MOMENTUM)
    self.ReLU = nn.ReLU()

    self.fc = nn.Linear(feature_size, n_classes, bias = False)

    self.n_classes = n_classes
    self.n_known = 0

    # Hyper-parameters from iCaRL
    self.BATCH_SIZE = BATCH_SIZE
    self.WEIGHT_DECAY  = WEIGHT_DECAY
    self.LR = LR
    self.GAMMA = GAMMA # this allow LR to become 1/5 LR after MILESTONES epochs
    self.NUM_EPOCHS = NUM_EPOCHS
    self.DEVICE = DEVICE
    self.MILESTONES = MILESTONES # when the LR decreases, according to icarl
    self.MOMENTUM = MOMENTUM
    self.K = K
    
    self.reverse_index=reverse_index

    self.optimizer, self.scheduler = utils.getOptimizerScheduler(self.LR, self.MOMENTUM, self.WEIGHT_DECAY, self.MILESTONES, self.GAMMA, self.parameters())

    gc.collect()
    

    # List containing exemplar_sets
    # Each exemplar_set is a np.array of N images
    # with shape (N, C, H, W)
    self.exemplar_sets = []

    # Learning method
    
    # for the classification loss we have two alternatives
    # 1- BCE loss with Logits (reduction could be mean or sum)
    # 2- BCE loss + sigmoid
    """self.cls_loss = nn.BCEWithLogitsLoss(reduction = 'mean')
                self.dist_loss = nn.BCEWithLogitsLoss(reduction = 'mean')"""
    
    

    # Means of exemplars
    self.compute_means = True
    self.exemplar_means = []

    
  def forward(self, x):
    x = self.feature_extractor(x)
    x = self.bn(x)
    x = self.ReLU(x)
    x = self.fc(x)

    return x
  
  # increment the number of classes considered by the net
  def increment_classes(self, n):
        gc.collect()
        """Add n classes in the final fc layer"""
        in_features = self.fc.in_features
        out_features = self.fc.out_features
        weight = self.fc.weight.data

        self.fc = nn.Linear(in_features, out_features + n, bias = False)
        self.fc.weight.data[:out_features] = weight
        self.n_classes += n

  # computes the means of each exemplar set
  def compute_means(self):
    torch.no_grad()  
    torch.cuda.empty_cache()

    exemplar_means = []
    feature_extractor = self.fc.to(DEVICE)
    feature_extractor.train(False)

    with torch.no_grad():
      for exemplar_set in self.exemplar_sets:
        features=[]
        for exemplar in exemplar_set:
          exemplar = exemplar.to(DEVICE)
          feature = feature_extractor(exemplar)
          features.append(feature)

          # cleaning 
          torch.no_grad()
          torch.cuda.empty_cache()

        features = torch.stack(features) # (num_exemplars,num_features)
        mean_exemplar = features.mean(0) 
        mean_exemplar.data = mean_exemplar.data / mean_exemplar.data.norm() # Normalize
        mean_exemplar = mean_exemplar.to('cpu')
        exemplar_means.append(mean_exemplar)

        # cleaning
        torch.no_grad()  
        torch.cuda.empty_cache()

    self.exemplar_means = exemplar_means


  def classify(self, batch_imgs):
      """Classify images by neares-means-of-exemplars
      Args:
          batch_imgs: input image batch
      Returns:
          preds: Tensor of size (batch_size,)
      """
      torch.no_grad()
      torch.cuda.empty_cache()

      batch_imgs_size = batch_imgs.size(0)
      feature_extractor = self.fc.to(DEVICE)
      feature_extractor.train(False)

      means_exemplars = torch.cat(self.exemplar_means, dim=0)
      means_exemplars = torch.stack([means_exemplars] * batch_imgs_size)
      means_exemplars = means.transpose(1, 2) 

      feature = feature_extractor(batch_imgs) 
      aus_normalized_features = []
      for el in feature: # Normalize
          el.data = el.data / el.data.norm()
          aus_normalized_features.append(el)

      feature = torch.stack(aus_normalized_features,dim=0)

      feature = feature.unsqueeze(2) 
      feature = feature.expand_as(means_exemplars) 

      means_exemplars = means_exemplars.to(DEVICE)
      # Nearest prototype
      preds = torch.argmin((feature - means_exemplars).pow(2).sum(1),dim=1)

      # cleaning
      torch.no_grad()
      torch.cuda.empty_cache()
      gc.collect()

      return preds

  # implementation of alg. 4 of icarl paper
  # iCaRL ConstructExemplarSet
  def construct_exemplar_set(self, tensors, m, transform):
    torch.no_grad()
    torch.cuda.empty_cache()
    gc.collect()

    feature_extractor = self.fc.to(DEVICE)
    feature_extractor.train(False)

    """Construct an exemplar set for image set
    Args:
        images: np.array containing images of a class
    """
    # Compute and cache features for each example
    features = []
    """
    for img in images:
                    x = Variable(transform(Image.fromarray(img)), volatile=True).cuda()
                    feature = self.feature_extractor(x.unsqueeze(0)).data.cpu().numpy()
                    feature = feature / np.linalg.norm(feature) # Normalize
                    features.append(feature[0])
    """
    loader = DataLoader(tensors,batch_size=BATCH_SIZE,shuffle=True,drop_last=False,num_workers = 4)

    for _, images, labels in loader:
      images = images.to(DEVICE)
      labels = labels.to(DEVICE)
      feature = self.feature_extractor(images) 

      # is this line important? it yields an error
      #feature = feature / np.linalg.norm(feature) # Normalize

      features.append(feature)

    features_s = torch.cat(features)
    class_mean = features_s.mean(0)
    class_mean = torch.stack([mean]*features_s.size()[0])
    torch.cuda.empty_cache()

    exemplar_set = []
    exemplar_features = [] # list of Variables of shape (feature_size,)
    for k in range(1, (m + 1)):
        S = torch.cat([summa]*features_s.size()[0]) # second addend, features in the exemplar set
        i = torch.argmin((mean-(1/k)*(features_s + S)).pow(2).sum(1),dim=0)
        exemplar_k = tensors[i.item()][0].unsqueeze(dim=0) # take the image
        exemplar_set.append(exemplar_k)
        phi =  feature_extractor(exemplar_k.to(DEVICE))
        summa = summa + phi # update sum of features
        del exemplar_k 

    # cleaning
    torch.cuda.empty_cache()
    self.exemplar_sets.append(exemplar_set) #update exemplar sets with the updated exemplars images


  def augment_dataset_with_exemplars(self, dataset):
    transformToImg = transforms.ToPILImage()
    for y, P_y in enumerate(self.exemplar_sets): #for each class and exemplar set for that class
        exemplar_images = P_y
        exemplar_labels = [y] * len(P_y) #i create a vector of labels [class class class ...] for each class in the exemplar set
        for exemplar in exemplar_images:
            exemplar = transformToImg(exemplar.squeeze()).convert("RGB")
            dataset.append(exemplar_images, y)


  def _one_hot_encode(self, labels, dtype=None, device=None):
    enconded = torch.zeros(self.n_classes, len(labels), dtype=dtype, device=device)
    for i, l in enumerate(labels):
      enconded[i, l] = 1
    return enconded

  # just a start to make the test work
  def update_representation(self, dataset, new_classes):
    # 1 - retrieve the classes from the dataset (which is the current train_subset)
    # 2 - retrieve the new classes
    # 1,2 are done in the main_icarl
    gc.collect()

    # 3 - increment classes
    #          (add output nodes)
    #          (update n_classes)
    self.increment_classes(len(new_classes))

    # 4 - combine current train_subset (dataset) with exemplars
    #     to form a new augmented train dataset
    self.augment_dataset_with_exemplars(dataset)

    # define the loader for the augmented_dataset
    loader = DataLoader(dataset, batch_size=self.BATCH_SIZE,shuffle=True, num_workers=4, drop_last = True)

    self.cuda()
    # 5 - store network outputs with pre-update parameters => q
    """    
    q = torch.zeros(len(dataset), self.n_classes)
    for indices, images, labels in loader:
        images = images.to(self.DEVICE)
        labels = labels.to(self.DEVICE)
        indices = indices.to(self.DEVICE)
        g = nn.functional.sigmoid(self.forward(images))
        q_i = g.data
        q[indices] = q_i
    """
    # 6 - run network training, with loss function

    net = self.feature_extractor
    net = net.to(self.DEVICE)

    optimizer = self.optimizer
    scheduler = self.scheduler

    criterion = utils.getLossCriterion()

    if self.n_known > 0:
        #old_net = copy.deepcopy(self.feature_extractor) #copy network before training
        old_net = copy.deepcopy(self) #test


    cudnn.benchmark # Calling this optimizes runtime
    #current_step = 0
    for epoch in range(self.NUM_EPOCHS):
        print("NUM_EPOCHS: ",epoch,"/", self.NUM_EPOCHS)
        for indices, images, labels in loader:
            # Bring data over the device of choice
            images = images.to(self.DEVICE)
            #labels = self._one_hot_encode(labels, device=self.DEVICE)
            labels = labels.to(self.DEVICE)
            indices = indices.to(self.DEVICE)
            net.train()

            # PyTorch, by default, accumulates gradients after each backward pass
            # We need to manually set the gradients to zero before starting a new iteration
            optimizer.zero_grad() # Zero-ing the gradients

            # Forward pass to the network
            outputs = self.forward(images)

            #loss = sum(self.cls_loss(g[:,y], labels[:,y]) for y in range(self.n_known, self.n_classes))
            labels_one_hot = utils._one_hot_encode(labels,self.n_classes, self.reverse_index, device=self.DEVICE)
            labels_one_hot.type_as(outputs)

            # test
            #labels_one_hot = nn.functional.one_hot(labels, self.n_classes)
            # Classification loss for new classes

            # Loss = only classification on new classes
            if self.n_known == 0:
                loss = criterion(outputs, labels_onehot)
            # Distilation loss for old classes, class loss on new classes
            if self.n_known > 0:
                # g = F.sigmoid(g)
                q_i = q[indices]
                # to check!
                for y in range(0,len(self.exemplar_sets)):
                    dist_loss += self.dist_loss(g[:,y],q_i[:,y])
                #dist_loss = dist_loss / self.n_known
                loss += dist_loss

            loss.backward()
            optimizer.step()

        scheduler.step()

    gc.collect()
    del net
    torch.no_grad()
    torch.cuda.empty_cache()


  # implementation of alg. 5 of icarl paper
  # iCaRL ReduceExemplarSet
  def reduce_exemplar_sets(self, m):
        for y, P_y in enumerate(self.exemplar_sets):
            # i keep only the first m exemplar images
            # where m is the UPDATED K/number_classes_seen
            # the number of images per each exemplar set (class) progressively decreases
            self.exemplar_sets[y] = P_y[:m] 