import random
import time
import argparse
from collections import deque, defaultdict, namedtuple
import copy
from policy_value_network_tf2 import *
from policy_value_network_gpus_tf2 import *
from threading import Lock
import gc


def create_uci_labels():
    labels_array = []
    for i in range(6):
        for j in range(6):
            for k in range(6):
                for l in range(6):
                    if bool(i != k) or bool(j != l):
                        labels_array.append(str(i) + str(j) + str(k) + str(l))
    return labels_array


def clear_tree(root, act):
    for child in root.child.items():
        if child[1] is not root.child[act]:
            clear_tree_recursive(child)

def clear_tree_recursive(subroot):
    if subroot[1].child == {}:
        subroot[1].parent = None
        del subroot
        return
    else:
        for child in subroot[1].child.items():
            clear_tree_recursive(child)
            child[1].parent = None
            del child


def make_move(move, board):
    origin = board.board[move.to_x][move.to_y]
    player = board.board[move.from_x][move.from_y]
    board.board[move.from_x][move.from_y] = 0
    board.board[move.to_x][move.to_y] = player
    if origin is black_chess:
        board.blackNum -= 1
    elif origin is white_chess:
        board.whiteNum -= 1
    return board

def is_kill_move(state_prev, state_next):
    return state_next.blackNum - state_prev.blackNum + state_next.whiteNum - state_prev.whiteNum

labels_array = create_uci_labels()
labels_len = len(labels_array)
label2i = {val: i for i, val in enumerate(labels_array)}
black_chess = -1
white_chess = 1


class Move(object):
    def __init__(self, from_x, from_y, to_x, to_y):
        self.from_x = from_x
        self.from_y = from_y
        self.to_x = to_x
        self.to_y = to_y

    def __eq__(self, other):
        return self.from_x == other.from_x and self.from_y == other.from_y and self.to_x == other.to_x and self.to_y == other.to_y


class Chess(object):
    def __init__(self, player, x, y):
        self.player = player
        self.x = x
        self.y = y


QueueItem = namedtuple("QueueItem", "feature future")
c_PUCT = 5
virtual_loss = 3
cut_off_depth = 30


class leaf_node(object):
    def __init__(self, in_parent, in_prior_p, board, board_stack):
        self.P = in_prior_p
        self.Q = 0
        self.N = 0
        self.v = 0
        self.U = 0
        self.W = 0
        self.parent = in_parent
        self.child = {}
        self.board = board
        if board_stack != {}:
            self.board_stack = board_stack
        else:
            a = GameBoard()
            self.board_stack = [[], []]
            for i in range(8):
                self.board_stack[0].append(a.board)
                self.board_stack[1].append(a.board)

    def is_leaf(self):
        return self.child == {}

    def get_Q_plus_U_new(self, c_puct):
        u = c_puct * self.P * np.sqrt(self.parent.N) / (1 + self.N)
        return self.Q + u

    def get_Q_plus_U(self, c_puct):
        self.U = c_puct * self.P * np.sqrt(self.parent.N) / (1 + self.N)
        return self.Q + self.U

    def select_new(self, c_puct):
        return max(self.child.items(), key=lambda node: node[1].get_Q_plus_U_new(c_puct))

    def select(self, c_puct):
        return max(self.child.items(), key=lambda node: node[1].get_Q_plus_U(c_puct))

    def expand(self, moves, action_probs):
        tot_p = 1e-8
        action_probs = tf.squeeze(action_probs)
        player = self.board.board[moves[0].from_x][moves[0].from_y]
        board_stack = copy.deepcopy(self.board_stack)
        for action in moves:
            board = make_move(action, copy.deepcopy(self.board))
            if player == 1:
                board_stack[1].append(board.board)
            else:
                board_stack[0].append(board.board)
            mov_p = action_probs[label2i[str(action.from_x) + str(action.from_y) + str(action.to_x) + str(action.to_y)]]
            new_node = leaf_node(self, mov_p, board, copy.deepcopy([board_stack[0][-8:], board_stack[1][-8:]]))
            self.child[str(action.from_x) + str(action.from_y) + str(action.to_x) + str(action.to_y)] = new_node
            tot_p += mov_p
            if player == 1:
                del board_stack[1][-1]
            else:
                del board_stack[0][-1]
        for a, n in self.child.items():
            n.P /= tot_p

    def back_up_value(self, value):
        self.N += 1
        self.W += value
        self.v = value
        self.Q = self.W / self.N
        self.U = c_PUCT * self.P * np.sqrt(self.parent.N) / (1 + self.N)

    def backup(self, value):
        node = self
        while node != None:
            node.N += 1
            node.W += value
            node.v = value
            node.Q = node.W / node.N  # node.Q += 1.0*(value - node.Q) / node.N
            node = node.parent
            value = -value


class MCTS_tree(object):
    def __init__(self, board, in_forward, search_threads):
        self.noise_eps = 0.25
        self.dirichlet_alpha = 0.3
        self.p_ = (1 - self.noise_eps) * 1 + self.noise_eps * np.random.dirichlet([self.dirichlet_alpha])
        self.root = leaf_node(None, self.p_, board, {})
        self.c_puct = 5
        self.forward = in_forward
        self.node_lock = defaultdict(Lock)

        self.virtual_loss = 3
        self.expanded = set()
        self.cut_off_depth = 30
        self.running_simulation_num = 0

    def init_b_r(self):
        self.board_record = []
        board_b = [
            [[-1, -1, -1, -1, -1, -1], [-1, -1, -1, -1, -1, -1], [0, 0, 0, 0, 0, 0], [0, 0, 0, 0, 0, 0],
             [1, 1, 1, 1, 1, 1],
             [1, 1, 1, 1, 1, 1]] * 8]
        board_w = [
            [[-1, -1, -1, -1, -1, -1], [-1, -1, -1, -1, -1, -1], [0, 0, 0, 0, 0, 0], [0, 0, 0, 0, 0, 0],
             [1, 1, 1, 1, 1, 1],
             [1, 1, 1, 1, 1, 1]] * 8]
        self.board_record.append(board_b)
        self.board_record.append(board_w)

    def update_b_r(self, board, current_player=1):
        index_r = 1 + current_player >> 32
        self.board_record[index_r].insert(0, board)
        self.board_record[index_r].pop()

    def reload(self, board):
        self.root = leaf_node(None, self.p_, board, {})
        self.expanded = set()

    def Q(self, move) -> float:  # type hint, Q() returns float
        ret = 0.0
        find = False
        for a, n in self.root.child.items():
            if move == a:
                ret = n.Q
                find = True
        if find is False:
            print("{} not exist in the child".format(move))
        return ret

    def state_to_positions(self, board, current_player):
        self.update_b_r(board, current_player)
        return self.board_record  # 6 * 6 * 16

    #@profile(precision=4)
    def update_tree(self, act):
        # if(act in self.root.child):
        self.expanded.discard(self.root)
        next_root = self.root.child[act]
        next_root.parent = None
        clear_tree(self.root, act)
        self.root = next_root
        gc.collect()
        #self.clear_tree(ori_root, act)



    def is_expanded(self, key) -> bool:
        return key in self.expanded

    def tree_search(self, node, current_player, restrict_round) -> float:

        if not self.is_expanded(node):
            positions = self.generate_inputs(node.board_stack, current_player)
            action_probs, value = self.forward(positions)
            moves = GameBoard.move_generate(node.board, current_player)
            node.expand(moves, action_probs)
            self.expanded.add(node)
            return value[0] * -1

        else:
            """node has already expanded. Enter select phase."""
            # select child node with maximum action scroe
            #last_board = copy.deepcopy(node.board)

            action, node = node.select_new(c_PUCT)
            current_player = -current_player
            '''
            if is_kill_move(last_board, node.board) == 0:
                restrict_round += 1
            else:
                restrict_round = 0
            '''
            node.N += virtual_loss
            node.W += -virtual_loss

            if node.board.judge(current_player) != 0:

                value = 1.0 if node.board.judge(current_player) == 1 else -1.0
                value = -1.0 if node.board.judge(current_player) == -1 else 1.0
                value = value * -1

            elif restrict_round >= 60:
                value = 0.0
            else:
                value = self.tree_search(node, current_player, restrict_round)  # next move
            node.N += -virtual_loss
            node.W += virtual_loss

            node.back_up_value(value)
            return value * -1

    #@profile(precision=4)
    def main(self, board_stack, current_player, restrict_round, playouts):
        node = self.root
        if not self.is_expanded(node):
            node.board_stack = board_stack
            positions = self.generate_inputs(node.board_stack, current_player)
            positions = np.expand_dims(positions, 0)
            action_probs, value = self.forward(positions)

            moves = GameBoard.move_generate(GameBoard(), current_player)
            node.expand(moves, action_probs)
            self.expanded.add(node)

        for _ in range(playouts):
            self.tree_search(node, current_player, restrict_round)

    def do_simulation(self, board, current_player, restrict_round):
        node = self.root
        #last_board = copy.deepcopy(board)
        while (node.is_leaf() == False):
            # print("do_simulation while current_player : ", current_player)
            action, node = node.select(self.c_puct)
            current_player = -current_player
            '''
            if is_kill_move(last_board, node.board) == 0:
                restrict_round += 1
            else:
                restrict_round = 0
                '''
            last_board = node.board

        positions = self.generate_inputs(node.board, current_player)
        positions = np.expand_dims(positions, 0)
        action_probs, value = self.forward(positions)

        if node.board.judge(current_player) != 0:

            value = 1.0 if node.board.judge(current_player) == 1 else -1.0
            value = -1.0 if node.board.judge(current_player) == -1 else 1.0
            value = value * -1
        elif restrict_round >= 60:
            value = 0.0
        else:
            moves = GameBoard.move_generate(node.board, current_player)
            node.expand(moves, action_probs)

        node.backup(-value)

    def generate_inputs(self, board_stack, current_player):
        inputs = np.zeros([6, 6, 17])
        if current_player == 1:
            for i in range(8):
                for j in range(6):
                    for k in range(6):
                        if board_stack[0][7 - i][j][k] == -current_player:
                            inputs[j][k][16 - i] = 1
            for i in range(8):
                for j in range(6):
                    for k in range(6):
                        if board_stack[1][7 - i][j][k] == current_player:
                            inputs[j][k][8 - i] = 1
            for i in range(6):
                for j in range(6):
                    inputs[i][j][0] = 1
        else:
            for i in range(8):
                for j in range(6):
                    for k in range(6):
                        if board_stack[1][7 - i][j][k] is -current_player:
                            inputs[j][k][16 - i] = 1
            for i in range(8):
                for j in range(6):
                    for k in range(6):
                        if board_stack[0][7 - i][j][k] is current_player:
                            inputs[j][k][8 - i] = 1
            for i in range(6):
                for j in range(6):
                    inputs[i][j][0] = -1
        return inputs


class GameBoard(object):
    board = [[-1, -1, -1, -1, -1, -1], [-1, -1, -1, -1, -1, -1], [0, 0, 0, 0, 0, 0],
             [0, 0, 0, 0, 0, 0], [1, 1, 1, 1, 1, 1], [1, 1, 1, 1, 1, 1]]

    blackNum = 12
    whiteNum = 12

    def __init__(self):
        self.round = 1
        self.player = white_chess
        self.restrict_round = 0

    @staticmethod
    def print_board(board):
        for i in range(6):
            for j in range(6):
                print(board[i][j], " ", end="")
            print()

    def judge(self, current_player):
        if current_player == -1:
            if self.blackNum == 0:
                return -1
            elif self.whiteNum == 0:
                return 1
            else:
                return 0
        elif current_player == 1:
            if self.blackNum == 0:
                return 1
            elif self.whiteNum == 0:
                return -1
            else:
                return 0

    @staticmethod
    def move_generate(game_board, current_player):
        moves = []

        inside_rool = 1
        exterior_rool = 2
        inrool_s, inrool_chess_s = GameBoard.extract_rool(game_board.board, inside_rool)
        exrool_s, exrool_chess_s = GameBoard.extract_rool(game_board.board, exterior_rool)

        GameBoard.attack_generate(moves, exrool_s, exrool_chess_s, current_player)
        GameBoard.attack_generate(moves, inrool_s, inrool_chess_s, current_player)

        for i in range(6):
            for j in range(6):
                if game_board.board[i][j] is current_player:
                    for k in range(i - 1, i + 2):
                        for l in range(j - 1, j + 2):
                            if 0 <= k <= 5:
                                if 0 <= l <= 5:
                                    if i is not k or j is not l:
                                        if game_board.board[k][l] == 0:
                                            moves.append(Move(i, j, k, l))

        for _ in range(len(exrool_s)):
            try:
                exrool_s.remove([0, 0, 0, 0, 0, 0])
            except:
                pass

        return moves

    @staticmethod
    def extract_rool(board, index):
        exrool_s = []
        exrool = []
        exrool_chess = []
        exrool_chess_s = []

        for i in range(6):
            exrool.append(board[index][i])
            exrool_chess.append(Chess(board[index][i], index, i))
        exrool_s.append(exrool)
        exrool_chess_s.append(exrool_chess)

        exrool = []
        exrool_chess = []
        for i in range(6):
            exrool.append(board[i][5 - index])
            exrool_chess.append(Chess(board[i][5 - index], i, 5 - index))
        exrool_s.append(exrool)
        exrool_chess_s.append(exrool_chess)

        exrool = []
        exrool_chess = []
        for i in reversed(range(6)):
            exrool.append(board[5 - index][i])
            exrool_chess.append(Chess(board[5 - index][i], 5 - index, i))
        exrool_s.append(exrool)
        exrool_chess_s.append(exrool_chess)

        exrool = []
        exrool_chess = []
        for i in reversed(range(6)):
            exrool.append(board[i][index])
            exrool_chess.append(Chess(board[i][index], i, index))
        exrool_s.append(exrool)
        exrool_chess_s.append(exrool_chess)

        for _ in range(len(exrool_s)):
            try:
                exrool_chess_s.pop(exrool_s.index([0, 0, 0, 0, 0, 0]))
                exrool_s.remove([0, 0, 0, 0, 0, 0])
            except:
                pass

        return exrool_s, exrool_chess_s

    @staticmethod
    def attack_generate(moves, rools, rools_chess, who):

        for i in range(len(rools)):
            p = -1
            for j in reversed(range(6)):
                if rools[i][j] is who:
                    p = j
                    break
                elif rools[i][j] is -who:
                    break
            if p < 0:
                continue

            p_n = -1
            for j in range(6):
                if rools[(i + 1) % (len(rools))][j] is -who:
                    p_n = j
                    break
                elif rools[(i + 1) % (len(rools))][j] is who:
                    break

            if p_n == -1:
                try:
                    p_n = rools[(i + 1) % (len(rools))].index(who)
                    if rools_chess[i][p].x is rools_chess[(i + 1) % (len(rools))][p_n].x and rools_chess[i][p].y is \
                            rools_chess[(i + 1) % (len(rools))][
                                p_n].y:
                        flag = False
                        for j in range(p_n + 1, 6):
                            if rools[(i + 1) % (len(rools))][j] is who:
                                break
                            elif rools[(i + 1) % (len(rools))][j] is -who:
                                p_n = j
                                flag = True
                                break
                        if flag is True:
                            if Move(rools_chess[i][p].x, rools_chess[i][p].y,
                                    rools_chess[(i + 1) % (len(rools))][p_n].x,
                                    rools_chess[(i + 1) % (len(rools))][p_n].y) not in moves:
                                moves.append(Move(rools_chess[i][p].x, rools_chess[i][p].y,
                                                  rools_chess[(i + 1) % (len(rools))][p_n].x,
                                                  rools_chess[(i + 1) % (len(rools))][p_n].y))
                except:
                    pass

            else:
                if Move(rools_chess[i][p].x, rools_chess[i][p].y, rools_chess[(i + 1) % (len(rools))][p_n].x,
                        rools_chess[(i + 1) % (len(rools))][p_n].y) not in moves:
                    moves.append(
                        Move(rools_chess[i][p].x, rools_chess[i][p].y, rools_chess[(i + 1) % (len(rools))][p_n].x,
                             rools_chess[(i + 1) % (len(rools))][p_n].y))

        for i in range(len(rools)):
            p = -1
            for j in range(6):
                if rools[i][j] is who:
                    p = j
                    break
                elif rools[i][j] is -who:
                    break
            if p < 0:
                continue

            p_n = -1
            for j in reversed(range(6)):
                if rools[i - 1][j] is -who:
                    p_n = j
                    break
                elif rools[i - 1][j] is who:
                    break

            if p_n == -1:
                try:
                    for j in reversed(range(6)):
                        if rools[i - 1][j] is who:
                            p_n = j
                            break
                    if rools_chess[i][p].x is rools_chess[i - 1][p_n].x and rools_chess[i][p].y is \
                            rools_chess[i - 1][
                                p_n].y:
                        flag = False
                        for j in reversed(range(0, p_n)):
                            if rools[i - 1][j] is who:
                                break
                            elif rools[i - 1][j] is -who:
                                p_n = j
                                flag = True
                                break
                        if flag is True:
                            if Move(rools_chess[i][p].x, rools_chess[i][p].y, rools_chess[i - 1][p_n].x,
                                    rools_chess[i - 1][p_n].y) not in moves:
                                moves.append(
                                    Move(rools_chess[i][p].x, rools_chess[i][p].y, rools_chess[i - 1][p_n].x,
                                         rools_chess[i - 1][p_n].y))
                except:
                    pass
            else:

                if Move(rools_chess[i][p].x, rools_chess[i][p].y, rools_chess[i - 1][p_n].x,
                        rools_chess[i - 1][p_n].y) not in moves:
                    moves.append(Move(rools_chess[i][p].x, rools_chess[i][p].y, rools_chess[i - 1][p_n].x,
                                      rools_chess[i - 1][p_n].y))

        return moves


    def is_game_over(self):
        if self.blackNum == 0:
            return -1
        elif self.whiteNum == 0:
            return 1
        else:
            return False

    def reload(self):
        self.board = [[-1, -1, -1, -1, -1, -1], [-1, -1, -1, -1, -1, -1], [0, 0, 0, 0, 0, 0],
                      [0, 0, 0, 0, 0, 0], [1, 1, 1, 1, 1, 1], [1, 1, 1, 1, 1, 1]]

        self.blackNum = 12
        self.whiteNum = 12
        self.round = 1
        self.player = white_chess
        self.restrict_round = 0


class surakarta(object):

    def __init__(self, playout=400, in_batch_size=128, exploration=True, search_threads=16, processor="cpu",
                 num_gpus=1, res_block_nums=7, human_color=black_chess):
        self.epochs = 5
        self.playout_counts = playout  # 400    #800    #1600    200
        self.temperature = 1  # 1e-8    1e-3
        # self.c = 1e-4
        self.batch_size = in_batch_size  # 128    #512
        # self.momentum = 0.9
        self.game_batch = 400  # Evaluation each 400 times
        # self.game_loop = 25000
        self.top_steps = 30
        self.top_temperature = 1  # 2
        # self.Dirichlet = 0.3    # P(s,a) = (1 - ϵ)p_a  + ϵη_a    #self-play chapter in the paper
        self.eta = 0.03
        # self.epsilon = 0.25
        # self.v_resign = 0.05
        # self.c_puct = 5
        self.learning_rate = 0.001  # 5e-3    #    0.001
        self.lr_multiplier = 1.0  # adaptively adjust the learning rate based on KL
        self.buffer_size = 10000
        self.data_buffer = deque(maxlen=self.buffer_size)
        self.game_borad = GameBoard()
        self.processor = processor
        self.policy_value_netowrk = policy_value_network(self.lr_callback,
                                                         res_block_nums) if processor == 'cpu' else policy_value_network_gpus(
            num_gpus, res_block_nums)
        self.mcts = MCTS_tree(self.game_borad, self.policy_value_netowrk.forward, search_threads)
        self.exploration = exploration
        self.resign_threshold = -0.8  # 0.05
        self.global_step = 0
        self.kl_targ = 0.025
        self.log_file = open(os.path.join(os.getcwd(), 'log_file.txt'), 'w')
        self.human_color = human_color

    def lr_callback(self):
        return self.learning_rate * self.lr_multiplier

    def policy_update(self):
        """update the policy-value net"""
        mini_batch = random.sample(self.data_buffer, self.batch_size)
        state_batch = [data[0] for data in mini_batch]
        mcts_probs_batch = [data[1] for data in mini_batch]
        winner_batch = [data[2] for data in mini_batch]

        winner_batch = np.expand_dims(winner_batch, 1)

        start_time = time.time()
        old_probs, old_v = self.mcts.forward(state_batch)
        for i in range(self.epochs):
            state_batch = np.array(state_batch)
            if len(state_batch.shape) == 3:
                sp = state_batch.shape
                state_batch = np.reshape(state_batch, [1, sp[0], sp[1], sp[2]])
            if self.processor == 'cpu':
                accuracy, loss, self.global_step = self.policy_value_netowrk.train_step(state_batch, mcts_probs_batch,
                                                                                        winner_batch,
                                                                                        self.learning_rate * self.lr_multiplier)  #
            else:
                with self.policy_value_netowrk.strategy.scope():
                    train_dataset = tf.data.Dataset.from_tensor_slices(
                        (state_batch, mcts_probs_batch, winner_batch)).batch(
                        len(winner_batch))  # , self.learning_rate * self.lr_multiplier
                    train_iterator = self.policy_value_netowrk.strategy.make_dataset_iterator(train_dataset)
                    train_iterator.initialize()
                    accuracy, loss, self.global_step = self.policy_value_netowrk.distributed_train(train_iterator)

            new_probs, new_v = self.mcts.forward(state_batch)
            kl_tmp = old_probs * (np.log((old_probs + 1e-10) / (new_probs + 1e-10)))

            kl_lst = []
            for line in kl_tmp:
                all_value = [x for x in line if str(x) != 'nan' and str(x) != 'inf']
                kl_lst.append(np.sum(all_value))
            kl = np.mean(kl_lst)

            if kl > self.kl_targ * 4:  # early stopping if D_KL diverges badly
                break
        self.policy_value_netowrk.save(self.global_step)
        print("train using time {} s".format(time.time() - start_time))

        # adaptively adjust the learning rate
        if kl > self.kl_targ * 2 and self.lr_multiplier > 0.1:
            self.lr_multiplier /= 1.5
        elif kl < self.kl_targ / 2 and self.lr_multiplier < 10:
            self.lr_multiplier *= 1.5

        explained_var_old = 1 - np.var(np.array(winner_batch) - tf.squeeze(old_v)) / np.var(
            np.array(winner_batch))  # .flatten()
        explained_var_new = 1 - np.var(np.array(winner_batch) - tf.squeeze(new_v)) / np.var(
            np.array(winner_batch))  # .flatten()
        print(
            "kl:{:.5f},lr_multiplier:{:.3f},loss:{},accuracy:{},explained_var_old:{:.3f},explained_var_new:{:.3f}".format(
                kl, self.lr_multiplier, loss, accuracy, explained_var_old, explained_var_new))
        self.log_file.write(
            "kl:{:.5f},lr_multiplier:{:.3f},loss:{},accuracy:{},explained_var_old:{:.3f},explained_var_new:{:.3f}".format(
                kl, self.lr_multiplier, loss, accuracy, explained_var_old, explained_var_new) + '\n')
        self.log_file.flush()
        # return loss, accuracy

    def run(self):
        batch_iter = 0
        try:
            while (True):
                batch_iter += 1
                boards, mcts_probs, z = self.selfplay()
                print("batch i:{}, episode_len:{}".format(batch_iter, len(z)))
                extend_data = []
                player = white_chess
                for i in reversed(range(len(z))):
                    board_stack = [boards[0][-8:], boards[1][-8:]]
                    states_data = self.mcts.generate_inputs(board_stack, player)
                    if player == white_chess:
                        del boards[1][-1]
                    else:
                        del boards[0][-1]
                    extend_data.append((states_data, mcts_probs[i], z[i]))
                    player = -player
                self.data_buffer.extend(extend_data)
                if len(self.data_buffer) > self.batch_size:
                    self.policy_update()
        except KeyboardInterrupt:
            self.log_file.close()
            self.policy_value_netowrk.save(self.global_step)

    def get_action(self, board_stack, temperature=1e-3):

        self.mcts.main(board_stack, self.game_borad.player, self.game_borad.restrict_round, self.playout_counts)

        actions_visits = [(act, nod.N) for act, nod in self.mcts.root.child.items()]
        actions, visits = zip(*actions_visits)
        probs = softmax(1.0 / temperature * np.log(visits))
        move_probs = []
        move_probs.append([actions, probs])

        if (self.exploration):
            act = np.random.choice(actions, p=0.75 * probs + 0.25 * np.random.dirichlet(0.3 * np.ones(len(probs))))
        else:
            act = np.random.choice(actions, p=probs)

        win_rate = self.mcts.Q(act)
        self.mcts.update_tree(act)
        return act, move_probs, win_rate

    def selfplay(self):
        self.game_borad.reload()
        self.mcts.reload(self.game_borad)
        boards, mcts_probs, current_players = [[], []], [], []
        z = None
        game_over = False
        a = GameBoard()
        for i in range(8):
            boards[0].append(a.board)
            boards[1].append(a.board)
        start_time = time.time()
        while not game_over:
            board_stack = [boards[0][-8:], boards[1][-8:]]
            action, probs, win_rate = self.get_action(board_stack, self.temperature)
            print(self.game_borad.round, self.game_borad.whiteNum, self.game_borad.blackNum)
            prob = np.zeros(labels_len)
            for idx in range(len(probs[0][0])):
                prob[label2i[probs[0][0][idx]]] = probs[0][1][idx]
            mcts_probs.append(prob)
            current_players.append(self.game_borad.player)

            #last_state = copy.deepcopy(self.game_borad)
            move = Move(int(action[0]), int(action[1]), int(action[2]), int(action[3]))
            self.game_borad = make_move(move, copy.deepcopy(self.game_borad))
            if self.game_borad.player is white_chess:
                boards[1].append(self.game_borad.board)
            else:
                boards[0].append(self.game_borad.board)
            self.game_borad.round += 1
            self.game_borad.player = -self.game_borad.player
            '''
            if is_kill_move(last_state, self.game_borad) == 0:
                self.game_borad.restrict_round += 1
            else:
                self.game_borad.restrict_round = 0
            '''
            if self.game_borad.is_game_over() != False:
                z = np.zeros(len(current_players))
                if (self.game_borad.is_game_over() == -1):
                    winner = -1
                elif (self.game_borad.is_game_over() == 1):
                    winner = 1
                z[np.array(current_players) == winner] = 1.0
                z[np.array(current_players) != winner] = -1.0
                game_over = True
                print("Game end. Winner is player : ", winner, " In {} steps".format(self.game_borad.round - 1))

            elif self.game_borad.restrict_round >= 60:
                z = np.zeros(len(current_players))
                game_over = True
                print("Game end. Tie in {} steps".format(self.game_borad.round - 1))

            if self.game_borad.round >= 2:
                return

            gc.collect()
        print("Using time {} s".format(time.time() - start_time))
        return boards, mcts_probs, z


def softmax(x):
    probs = np.exp(x - np.max(x))
    probs /= np.sum(probs)
    return probs


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', default='train', choices=['train', 'play'], type=str, help='train or play')
    parser.add_argument('--ai_count', default=1, choices=[1, 2], type=int, help='choose ai player count')
    parser.add_argument('--ai_function', default='mcts', choices=['mcts', 'net'], type=str, help='mcts or net')
    parser.add_argument('--train_playout', default=400, type=int, help='mcts train playout')
    parser.add_argument('--batch_size', default=512, type=int, help='train batch_size')  # 512
    parser.add_argument('--play_playout', default=400, type=int, help='mcts play playout')
    parser.add_argument('--delay', dest='delay', action='store',
                        nargs='?', default=3, type=float, required=False,
                        help='Set how many seconds you want to delay after each move')
    parser.add_argument('--end_delay', dest='end_delay', action='store',
                        nargs='?', default=3, type=float, required=False,
                        help='Set how many seconds you want to delay after the end of game')
    parser.add_argument('--search_threads', default=16, type=int, help='search_threads')
    parser.add_argument('--processor', default='cpu', choices=['cpu', 'gpu'], type=str, help='cpu or gpu')
    parser.add_argument('--num_gpus', default=1, type=int, help='gpu counts')
    parser.add_argument('--res_block_nums', default=7, type=int, help='res_block_nums')
    parser.add_argument('--human_color', default='b', choices=['w', 'b'], type=str, help='w or b')
    args = parser.parse_args()

    if args.mode == 'train':
        train_main = surakarta(args.train_playout, args.batch_size, True, args.search_threads, args.processor, args.num_gpus,
                               args.res_block_nums, args.human_color)  # * args.num_gpus
        train_main.selfplay()
    '''
    elif args.mode == 'play':
        from ChessGame_tf2 import *
        game = ChessGame(args.ai_count, args.ai_function, args.play_playout, args.delay, args.end_delay, args.batch_size,
                         args.search_threads, args.processor, args.num_gpus, args.res_block_nums, args.human_color)    # * args.num_gpus
        game.start()
        '''
