import copy
import os, sys

from models.agent import Agent

currentdir = os.path.dirname(os.path.realpath(__file__))
parentdir = os.path.dirname(currentdir)
sys.path.append(parentdir)

import torch
import torch.nn as nn
import numpy as np
import random
import pickle 

from gym_trading_btc.gym_anytrading.envs import *

from .replay_memory import Memory

class DQNSolver(nn.Module):
    def __init__(self, **kwargs):
        super(DQNSolver, self).__init__()

        self.batch_size = kwargs["batch_size"]
        self.n_channels = kwargs["num_features"]
        self.input_size = kwargs["window_size"]
        self.stride = kwargs["stride"]
        self.filter_size = kwargs["filter_sizes"]
        self.kernel_size = kwargs["kernel_sizes"]
        self.hidden_size_mlp = kwargs["hidden_size"]

        self.convolutional_block = torch.nn.Sequential()
        num_layers = len(kwargs["kernel_sizes"])
        for i in range(num_layers):
            in_val = self.n_channels if i==0 else self.filter_size[i]
            block = torch.nn.Sequential( 
                torch.nn.Conv2d(in_channels = in_val, 
                            out_channels= self.filter_size[1], 
                            kernel_size= [1, self.kernel_size[1]], 
                            stride=1, 
                            padding='same'),
                torch.nn.LeakyReLU(),
                torch.nn.Dropout(p = kwargs["dropout_conv"]),
                #torch.nn.MaxPool2d(kernel_size=kernel_size[i], stride = (1,1), padding=(0,0))
            )
            self.convolutional_block.add_module('conv_block_'+ str(i), copy.copy(block))

        self.gru_block = torch.nn.Sequential(
            torch.nn.GRU(
                input_size = int(self.input_size*self.filter_size[-1]/self.stride),
                hidden_size = kwargs["gru_cell_size"],
                num_layers =  kwargs["gru_num_cell"], 
                dropout =  kwargs["dropout_gru"], 
                bias = True,
                batch_first = True,
                bidirectional = False 
            )
        )

        self.mlp_block = torch.nn.Sequential()
        
        for ind in range(1, len(self.hidden_size_mlp)):
            block = torch.nn.Sequential(
                nn.Linear(in_features = self.hidden_size_mlp[ind-1], 
                          out_features =self.hidden_size_mlp[ind]),
                nn.LeakyReLU(),
                nn.Dropout(p = kwargs["dropout_linear"])
            )
            self.mlp_block.add_module('mlp_block_'+ str(ind), copy.copy(block))
        
        block_fin = torch.nn.Sequential(
                nn.Linear(kwargs["hidden_size"][-1], kwargs["num_actions"]),
                nn.Softmax(dim =1)
            )
        
        self.mlp_block.add_module('mlp_block_output', copy.copy(block_fin))
        
            
    def forward(self, x):

        y = torch.reshape(x, (x.shape[0], self.stride, int(self.input_size/self.stride), self.n_channels))
        y = torch.permute(y, (0, 3, 1, 2))  # To have the correct number of channels
        conv_res = self.convolutional_block(y)
        conv_res = torch.permute(conv_res, (0, 2, 1, 3))
        conv_res = torch.reshape(conv_res, (conv_res.shape[0], self.stride, conv_res.shape[2] * conv_res.shape[3]))
         
        gru_res, h = self.gru_block(conv_res)
       
        output_gru = torch.unbind(gru_res, dim=1)[-1] # to get the output of the last
        output_fin = self.mlp_block(output_gru)
        return output_fin

class DQNAgentDeepsense(Agent): 
    def __init__(self, **config):
        super().__init__(**config)
         
        #self.device = "cpu"
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        
        self.dqn_validation = DQNSolver(**config).to(self.device)
        self.dqn_target = DQNSolver(**config).to(self.device)
        
        self.lr = config["lr"]
        
        self.optimizer = torch.optim.RMSprop(self.dqn_validation.parameters(), lr = self.lr)
        self.loss = nn.HuberLoss().to(self.device)
        self.gamma = config["gamma"]
        
        self.memory_size = config["max_mem_size"]
        self.exploration_rate = config["exploration_rate"]      # To preserve from getting stuck
        self.exploration_decay =config["exploration_decay"] 
        self.exploration_min = config["exploration_min"]
        self.replace_target = config["replace_target"]
        
        self.memory = Memory(self.memory_size)
        
        self.current_position = 0
        self.is_full = 0
        
        self.batch_size = config["batch_size"]
         
    def predict(self, state):
        if random.random() < self.exploration_rate:
            return random.randint(0, self.num_actions-1)
        else: 
            state = torch.from_numpy(state).float()
            state = state.unsqueeze(0)
            action = self.dqn_validation(state.to(self.device)).argmax().unsqueeze(0).unsqueeze(0).cpu()
            return action.item()
    
    def learn(self, previous_state, action, next_state, reward, terminal):
        self.remember(previous_state, action, reward, next_state, terminal)
        self.trading_lessons()

    def remember(self, state, action, reward, next_state, terminal):
        self.memory.append(state, action, reward, next_state, terminal)
    
    def learn_episode(self, num_episode, **kwargs):
        if num_episode % self.replace_target == 0:
            self.update_params()

    def print_infos(self):
        print("\n------------ Deepsense agent training on " + self.device + " epoch " + str(self.current_position) + " -------------")
        print("exploration_rate", self.exploration_rate)
        #print("Prediction for this step", action)
        #print("Target for this step", action)
        #print("Current for this step", action)
       
    def trading_lessons(self):
        
        # compute a random batch from the memory before and pass it, then retrop
        if self.memory.size > self.batch_size:
            state ,action ,reward ,issue ,term = self.memory.compute_batch(self.batch_size)
            
            self.optimizer.zero_grad()
            
            #Q - learning :  target = r + gam * max_a Q(S', a)
            
            state = state.to(self.device)
            action = action.to(self.device)
            reward = reward.to(self.device)
            issue = issue.to(self.device)
            #term = term.to(self.device)
            
            pred_next = self.dqn_target(issue)
            pred_eval = self.dqn_validation(issue).argmax(1)
            current_pred = self.dqn_validation(state)
            
            action_max = pred_eval.long()
            
            target = current_pred.clone()
            
            batch_index = torch.from_numpy(np.arange(self.batch_size, dtype=np.int32)).long()
            
            #target = reward + self.gamma*torch.mul(self.dqn(issue).max(1).values.unsqueeze(1) , 1-term)
            
            action = torch.ravel(action).long()
            reward = reward.ravel()
            term = term.ravel()
            
            target[batch_index, action] = reward + self.gamma*pred_next[batch_index, action_max]*(1-term)
            
            #current = self.dqn(state).gather(1, action.long())
            
            loss = self.loss(current_pred, target)
            loss.backward()
            self.optimizer.step()
            
            # Eventually reduce the exploration rate
            
            self.exploration_rate *= self.exploration_decay
            self.exploration_rate = max(self.exploration_rate, self.exploration_min)
    
    def save(self, name):
        with open(name+ "/deque.pkl", "wb") as f:
            pickle.dump(self.memory, f)
        with open(name+ "/ending_position.pkl", "wb") as f:
            pickle.dump(self.current_position, f)
        with open(name+ "/num_in_queue.pkl", "wb") as f:
            pickle.dump(self.is_full, f)
    
    def update_params(self):
        self.dqn_target.load_state_dict(self.dqn_validation.state_dict())

    def get_exploration(self):
        return self.exploration_rate