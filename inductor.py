import imp
import re
from copy import deepcopy
import numpy as np
import random
import argparse
from sklearn import cluster
import torch
import torch.nn.functional as F
from transformers import (AutoModelForSeq2SeqLM, AutoTokenizer,
                          BartForConditionalGeneration, BartTokenizer, BertModel, BertTokenizer)

from dpp_sampler import DPPsampler
from src.bart_with_group_beam import BartForConditionalGeneration_GroupBeam
from src.utils import (construct_template, filter_words,
                       formalize_tA, post_process_template, align)

ORION_HYPO_GENERATOR = 'chenxran/orion-hypothesis-generator'
ORION_INS_GENERATOR = 'chenxran/orion-instance-generator'

RELATIONS = [
    "Causes",
    "HasProperty",
    "MadeUpOf",
    "isAfter",
    "isBefore",
    "xReact",
    "xWant",
    "xReason",
    "xAttr",
    "Desires",
]


class BartInductor(object):
    def __init__(
        self,
        device='cuda:3', 
        group_beam=True,
        continue_pretrain_instance_generator=True,
        continue_pretrain_hypo_generator=True,
        if_then=False
    ):
        self.device = device
        self.if_then = if_then
        self.orion_instance_generator_path = 'facebook/bart-large' if not continue_pretrain_instance_generator else ORION_INS_GENERATOR
        self.orion_hypothesis_generator_path = 'facebook/bart-large' if not continue_pretrain_hypo_generator else ORION_HYPO_GENERATOR

        if group_beam:
            self.orion_hypothesis_generator = BartForConditionalGeneration_GroupBeam.from_pretrained(self.orion_hypothesis_generator_path).cuda(self.device).eval().half()
        else:
            self.orion_hypothesis_generator = BartForConditionalGeneration.from_pretrained(self.orion_hypothesis_generator_path).cuda(self.device).eval().half()
        
        self.orion_instance_generator = BartForConditionalGeneration.from_pretrained(self.orion_instance_generator_path).cuda(self.device).eval().half()
    
        self.tokenizer = BartTokenizer.from_pretrained("facebook/bart-large")
        self.word_length = 2

        self.dpp_sampler = DPPsampler(device)

        self.stop_sub_list = ['he', 'she', 'this', 'that', 'and', 'it', 'which', 'who', 'whose', 'there', 'they', '.', 'its', 'one',
                                'i', ',', 'the', 'nobody', 'his', 'her', 'also', 'only', 'currently', 'here', '()', 'what', 'where',
                                'why', 'a', 'some', '"', ')', '(', 'now', 'everyone', 'everybody', 'their', 'often', 'usually', 'you',
                                '-', '?', ';', 'in', 'on', 'each', 'both', 'him', 'typically', 'mostly', 'sometimes', 'normally',
                                'always', 'usually', 'still', 'today', 'was', 'were', 'but', 'although', 'current', 'all', 'have',
                                'has', 'later', 'with', 'most', 'nowadays', 'then', 'every', 'when', 'someone', 'anyone', 'somebody',
                                'anybody', 'any', 'being', 'get', 'getting', 'thus', 'under', 'even', 'for', 'can', 'rarely', 'never',
                                'may', 'generally', 'other', 'another', 'too', 'first', 'second', 'third', 'mainly', 'primarily',
                                'having', 'have', 'has']

        self.stop_size = len(self.stop_sub_list)
        for i in range(self.stop_size):
            if self.stop_sub_list[i][0].isalpha():
                temp = self.stop_sub_list[i][0].upper() + self.stop_sub_list[i][1:]
                self.stop_sub_list.append(temp)

        self.bad_words_ids = [self.tokenizer.encode(bad_word)[1:-1] for bad_word in ['also', ' also']]
        stop_index = self.tokenizer(self.stop_sub_list, max_length=4, padding=True)
        stop_index = torch.tensor(stop_index['input_ids'])[:, 1]
        stop_weight = torch.zeros(1, self.tokenizer.vocab_size).cuda(self.device)
        stop_weight[0, stop_index] -= 100
        self.stop_weight = stop_weight[0, :]

    def clean(self, text):
        segments = text.split('<mask>')
        if len(segments) == 3 and segments[2].startswith('.'):
            return '<mask>'.join(segments[:2]) + '<mask>.'
        else:
            return text

    def generate(self, inputs, k=10, topk=10):
        with torch.no_grad():
            tB_probs = self.generate_rule(inputs, k)
            #tB_probs = self.generate_rule_improved(inputs, k)
            ret = [t[0].replace('<ent0>','<mask>').replace('<ent1>','<mask>') for t in tB_probs]

            new_ret = []
            for temp in ret:
                temp = self.clean(temp.strip())
                if len(new_ret) < topk and temp not in new_ret:
                    new_ret.append(temp)

            return new_ret

    def explore_mask(self, tA, k, tokens, prob, required_token, probs):
        if required_token == 0:
            return [[tokens, prob, probs]]
        if required_token <= self.word_length:
            k = min(k, 2)
        ret = []
        generated_ids = self.tokenizer(tA, max_length=128, padding='longest', return_tensors='pt')  # ["input_ids"].cuda(self.device)
        for key in generated_ids.keys():
            generated_ids[key] = generated_ids[key].cuda(self.device)
        mask_index = torch.where(generated_ids["input_ids"][0] == self.tokenizer.mask_token_id)
        generated_ret = self.orion_instance_generator(**generated_ids)
        #logits = generated_ret.logits
        logits = generated_ret[0]
        softmax = F.softmax(logits, dim=-1)
        mask_word = softmax[0, mask_index[0][0], :] + self.stop_weight
        top_k = torch.topk(mask_word, k, dim=0)
        for i in range(top_k[1].size(0)):
            token_s = top_k[1][i]
            prob_s = top_k[0][i].item()
            token_this = self.tokenizer.decode([token_s]).strip()
            if token_this[0].isalpha() == False or len(token_this) <= 2:
                continue
            index_s = tA.index(self.tokenizer.mask_token)
            tAs = tA[:index_s] + token_this + tA[index_s + len(self.tokenizer.mask_token):]
            tokens_this = [t for t in tokens]
            tokens_this.append(token_this)
            probs_new = deepcopy(probs)
            probs_new.append(prob_s)
            ret.extend(self.explore_mask(tAs, 1, tokens_this, prob_s * prob, required_token - 1,probs_new))
        return ret

    def extract_words_for_tA_bart(self, tA, k=6, softmax=True):
        spans = [t.lower().strip() for t in tA[:-1].split('<mask>')]
        generated_ids = self.tokenizer([tA], padding='longest', return_tensors='pt')['input_ids'].cuda(self.device)
        generated_ret = self.orion_instance_generator.generate(generated_ids, num_beams=max(120, k),
                                            #num_beam_groups=max(120, k),
                                            max_length=generated_ids.size(1) + 15,
                                            num_return_sequences=max(120, k), #min_length=generated_ids.size(1),
                                            #diversity_penalty=2.0,
                                            #length_penalty= 0.8,
                                            #early_stopping=True, bad_words_ids=bad_words_ids, no_repeat_ngram_size=2,
                                            output_scores=True,
                                            return_dict_in_generate=True)
        summary_ids = generated_ret['sequences']
        if softmax:
            probs = F.softmax(generated_ret['sequences_scores'])
        else:
            probs = generated_ret['sequences_scores']
        txts = [self.tokenizer.decode(g, skip_special_tokens=True, clean_up_tokenization_spaces=True) for g in summary_ids]
        ret = []

        for i, txt in enumerate(txts):
            if tA.endswith('.'):
                if txt.endswith('.'):
                    txt = txt[:-1].strip()
                txt += '.'

            prob = probs[i].item()
            '''
            words_i = align(tA, txt)
            if '' in words_i:
                continue
            '''

            word_imcomplete = False
            words_i = []

            start_index = 0
            for j in range(len(spans)-1):
                span1 = spans[j]
                span2 = spans[j+1]
                if (span1 in txt.lower()[start_index:]) and (span2 in txt.lower()[start_index:]):
                    index1 = txt.lower().index(span1, start_index) + len(span1)
                    if span2 == '':
                        if txt[-1] == '.':
                            index2 = len(txt) -1
                        else:
                            index2 = len(txt)
                    else:
                        index2 = txt.lower().index(span2, start_index)

                    words_i.append(txt[index1:index2].strip())
                    start_index = index2
                    #if words_i[-1] == '':
                    #    word_imcomplete = True
                else:
                    word_imcomplete = True
            if word_imcomplete:
                # if print_it:
                    # print(txt + '\t' + tA + '\t' + '×')
                continue
    
            ret.append([words_i, prob])

        return sorted(ret, key=lambda x: x[1], reverse=True)[:k]


    def extract_words_for_tA(self, tA, k=6):
        word_mask_str = ' '.join([self.tokenizer.mask_token] * self.word_length)
        tA = tA.replace('<mask>', word_mask_str)
        mask_count = tA.count(self.tokenizer.mask_token)
        mask_probs = self.explore_mask(tA, k*20, [], 1.0, mask_count, [])
        ret = []
        visited_mask_txt = {}
        for mask, prob, probs in mask_probs:
            mask_txt = ' '.join(mask).lower()
            if mask_txt in visited_mask_txt:
                continue
            visited_mask_txt[mask_txt] = 1
            words = []
            probs_words = []
            for i in range(0,mask_count, self.word_length):
                words.append(' '.join(mask[i: i + self.word_length]))
                prob_word = 1.0
                for j in range(i, i + self.word_length):
                    prob_word *= probs[j]
                probs_words.append(prob_word)
            ret.append([words, prob, probs_words])
        return sorted(ret, key=lambda x: x[1], reverse=True)[:k]

    def extract_templateBs_batch(self, words_prob, tA, k, softmax=True):
        words_prob_sorted = []
        for (words, probA, *_) in words_prob:
            tokenized_word = self.tokenizer(words[0])
            words_prob_sorted.append([words,probA,len(tokenized_word['input_ids'])])
        words_prob_sorted.sort(key=lambda x:x[2])

        batch_size = 8
        templates = []
        index_words = {}
        ret = {}
        num_beams = k
        for enum, (words, probA, *_) in enumerate(words_prob_sorted):
            template = construct_template(words, tA, self.if_then)
            templates.extend(template)
            for t in template:
                index_words[len(index_words)] = '\t'.join(words)
            # index_words[len(templates)-1] = '\t'.join(words)
            if (len(templates) == batch_size) or enum==len(words_prob_sorted)-1 or (words_prob_sorted[enum+1][2]!=words_prob_sorted[enum][2]):
                generated_ids = self.tokenizer(templates, padding="longest", return_tensors='pt')['input_ids'].cuda(self.device)
                generated_ret = self.orion_hypothesis_generator.generate(generated_ids, num_beams=num_beams,
                                                    num_beam_groups=num_beams,
                                                    max_length=28, #template_length+5,
                                                    num_return_sequences=num_beams, min_length=3,
                                                    diversity_penalty=1.0,
                                                    early_stopping=True,
                                                    #length_penalty = 0.1,
                                                    bad_words_ids=self.bad_words_ids,
                                                    #no_repeat_ngram_size=2,
                                                    output_scores=True,
                                                    return_dict_in_generate=True, decoder_ori_input_ids = generated_ids,
                                                    top_p=0.95,
                                                    )
                summary_ids = generated_ret['sequences'].reshape((len(templates),num_beams,-1))
                if softmax:
                    probs = F.softmax(generated_ret['sequences_scores'].reshape((len(templates),num_beams)),dim=1)
                else:
                    probs = generated_ret['sequences_scores']
                for ii in range(summary_ids.size(0)):
                    txts = [self.tokenizer.decode(g, skip_special_tokens=True, clean_up_tokenization_spaces=True) for g in
                            summary_ids[ii]]
                    ii_template = []
                    words_ii = index_words[ii].split('\t')
                    for i, txt in enumerate(txts):
                        prob = probs[ii][i].item() * probA

                        txt = txt.lower()
                        txt = post_process_template(txt)

                        words_ii_matched = [word.lower() for word in words_ii] #extract_similar_words(txt, words_ii)
                        if words_ii_matched is None:
                            prob = 0.0
                        else:
                            for j, word in enumerate(words_ii_matched):
                                if word not in txt:
                                    prob = 0.0
                                else:
                                    txt = txt.replace(word, '<ent{}>'.format(j), 1)
                        
                        if not txt.endswith('<ent1>.'):
                            prob = 0.0

                        if txt.count(' ')+1<=3:
                            continue

                        ii_template.append([txt, prob])
                    # if print_it:
                        # print(index_words[ii]+'\t'+str(convert_for_print(ii_template)))
                    for template, prob in ii_template:
                        if template not in ret:
                            ret[template] = 0.0
                        ret[template] += prob
                templates.clear()
                index_words.clear()

        return ret #sorted(ret, key=lambda x: x[1], reverse=True) #ret

    def generate_rule(self, tA, k=10, print_it = False):
        tA = formalize_tA(tA)
        if 'bart' in str(self.orion_instance_generator.__class__).lower():
            words_prob = self.extract_words_for_tA_bart(tA, k, softmax=True)
            words_prob = filter_words(words_prob)[:k]
            # if print_it:
                # print(convert_for_print(words_prob))
        else:
            words_prob = self.extract_words_for_tA(tA, k)
            words_prob = filter_words(words_prob)[:k]

        tB_prob = self.extract_templateBs_batch(words_prob, tA, k)

        ret = []
        for k1 in tB_prob:
            ret.append([k1, tB_prob[k1]])
        ret = sorted(ret, key=lambda x: x[1], reverse=True)[:k]
        if self.if_then:
            for i, temp in enumerate(ret):
                sentence = temp[0]
                if "then" in sentence:
                    sentence = sentence.split("then")[-1]
                else:
                    sentence = sentence.replace("if", "")
                ret[i][0] = sentence
        return ret

    def generate_rule_improved(self, tA, k=10):
        tA = formalize_tA(tA)
        tA = tA.replace('<mask>', ' <mask> ').replace('  ', ' ')
        words_prob = self.extract_words_for_tA_bart(tA, k*10, softmax=True)
        words_prob = filter_words(words_prob)#[:k]

        #r = tA.split('<mask>')[1]
        #sents = [[word[0][0] + r + word[0][1], 1] for word in words_prob]
        sents = [[tA.replace('<mask>', word[0][0], 1).replace('<mask>', word[0][1], 1), 1] for word in words_prob]

        # method 1: clusting ins
        n_clusters = k//2
        clusters = []
        labels = self.dpp_sampler.Kmeans_clusting(sents, n_clusters) if len(sents) > n_clusters else list(range(len(sents)))
        for n in range(n_clusters):
            clusters.append([words_prob[i] for i in range(len(sents)) if labels[i] == n])
        
        rhs_scores = {}
        for cluster in clusters:
            if len(cluster) > 0:
                rhs_scores.update(self.extract_templateBs_batch(cluster, tA, k, softmax=True))

        # method 2: sample typical ins through DPP
        #selected_ids = self.dpp_sampler.dpp(sents, k)
        #selected_ins = [words_prob[i] for i in selected_ids]
        #rhs_scores = self.generate_rhs(selected_ins, tA, k)
        

        rhs_ls = [[key, rhs_scores[key]] for key in rhs_scores.keys() if rhs_scores[key] > 0]
        rhs_text = [[t[0].replace('<ent0>','').replace('<ent1>',''), t[1]] for t in rhs_ls]
        
        # rescoring!
        #rhs_ls = self.dpp_sampler.rescoring(rhs_text)

        if len(rhs_text) > 0:
            selected_ids = self.dpp_sampler.dpp(rhs_text, k)
            ret = [rhs_ls[id] for id in selected_ids]
        else:
            ret = [['no proper relation hypothesis', 0]]

        ret = sorted(ret, key=lambda x: x[1], reverse=True)[:k]
        if self.if_then:
            for i, temp in enumerate(ret):
                sentence = temp[0]
                if "then" in sentence:
                    sentence = sentence.split("then")[-1]
                else:
                    sentence = sentence.replace("if", "")
                ret[i][0] = sentence
        return ret

    def generate_rhs(self, words_prob, tA, k):
        rhs_scores = {}
        for word_prob in words_prob:
            rh_candidates = self.generate_rh(word_prob, tA, k)
            for candidate in rh_candidates.items():
                if candidate[0] not in rhs_scores.keys():
                    rhs_scores.update({candidate[0]:candidate[1]})
                else:
                    rhs_scores.update({candidate[0]:candidate[1]+rhs_scores[candidate[0]]})

        return rhs_scores

    def generate_rh(self, scored_ins, tA, k, print_it=False):
        ins, score = scored_ins
        template = construct_template(ins, tA, self.if_then)
        num_beams = k
        generated_ids = self.tokenizer(template, padding="longest", return_tensors='pt')['input_ids'].cuda(self.device)
        generated_ret = self.orion_hypothesis_generator.generate(generated_ids, num_beams=num_beams,
                                            num_beam_groups=num_beams,
                                            max_length=28, #template_length+5,
                                            num_return_sequences=num_beams, min_length=3,
                                            diversity_penalty=1.0,
                                            early_stopping=True,
                                            #length_penalty = 0.1,
                                            bad_words_ids=self.bad_words_ids,
                                            #no_repeat_ngram_size=2,
                                            output_scores=True,
                                            return_dict_in_generate=True, decoder_ori_input_ids = generated_ids,
                                            top_p=0.95,
                                            )
        summary_ids = generated_ret['sequences']
        scores = generated_ret['sequences_scores'] + score
        txts = [self.tokenizer.decode(g, skip_special_tokens=True, clean_up_tokenization_spaces=True) for g in summary_ids]
        ret = {}
        for i, txt in enumerate(txts):
            txt = txt.lower()
            txt = post_process_template(txt)
            score_rh = scores[i].tolist()
            words_ii_matched = [word.lower() for word in ins] #extract_similar_words(txt, words_ii)
            if words_ii_matched is None:
                score_rh = -1000.0
            else:
                for j, word in enumerate(words_ii_matched):
                    if word not in txt:
                        score_rh = -1000.0
                    else:
                        txt = txt.replace(word, '<ent{}>'.format(j), 1)
            ret.update({txt:np.exp(score_rh)})
        return ret

class CometInductor(object):
    def __init__(self):
        self.model = AutoModelForSeq2SeqLM.from_pretrained("adamlin/comet-atomic_2020_BART").cuda(self.device).eval() # .half()
        self.tokenizer = AutoTokenizer.from_pretrained("adamlin/comet-atomic_2020_BART")
        self.task = "summarization"
        self.use_task_specific_params()
        self.decoder_start_token_id = None

    def drop_repeat(self, old_list):
        new_list = []
        for item in old_list:
            if item not in new_list:
                new_list.append(item)
        
        return new_list

    def chunks(self, lst, n):
        """Yield successive n-sized chunks from lst."""
        for i in range(0, len(lst), n):
            yield lst[i : i + n]

    def use_task_specific_params(self):
        """Update config with summarization specific params."""
        task_specific_params = self.model.config.task_specific_params

        if task_specific_params is not None:
            pars = task_specific_params.get(self.task, {})
            self.model.config.update(pars)

    def trim_batch(
        self, input_ids, pad_token_id, attention_mask=None,
    ):
        """Remove columns that are populated exclusively by pad_token_id"""
        keep_column_mask = input_ids.ne(pad_token_id).any(dim=0)
        if attention_mask is None:
            return input_ids[:, keep_column_mask]
        else:
            return (input_ids[:, keep_column_mask], attention_mask[:, keep_column_mask])

    def generate(self, inputs, k, topk):
        outputs = []
        words = ['PersonX', 'PersonY']
        for i, _ in enumerate(re.findall("<mask>", inputs)):
            index = inputs.index('<mask>')
            inputs = inputs[:index] + words[i] + inputs[index + len('<mask>'):]

        for relation in RELATIONS:
            inputs = "{} {} [GEN]".format(inputs[:-1], relation)
            gen = self.generate_(inputs, num_generate=10)
            switch = 0
            for output in gen[0]:
                output = output.strip()
                if re.search("PersonX|X", output) and re.search("PersonY|Y", output):
                    temp = re.sub("PersonX|X|PersonY|Y", "<mask>", output.strip())
                    if temp.endswith("."):
                        outputs.append(temp)
                    else:
                        outputs.append(temp + ".")
                    switch = 1
                    break

            if switch == 0:
                output = gen[0][0]
                temp = re.sub("PersonX|X|PersonY|Y", "<mask>", output.strip())
                if temp.endswith("."):
                    outputs.append(temp)
                else:
                    outputs.append(temp + ".")
        
        outputs = [output.replace('PersonX', '<mask>').replace('PersonY', '<mask>') for output in outputs]
        return outputs

    def generate_(
            self, 
            queries,
            decode_method="beam", 
            num_generate=5, 
        ):

        with torch.no_grad():
            decs = []
            batch = self.tokenizer(queries, return_tensors="pt", padding="longest")
            input_ids, attention_mask = self.trim_batch(**batch, pad_token_id=self.tokenizer.pad_token_id)

            summaries = self.model.generate(
                input_ids=input_ids.cuda(self.device),
                attention_mask=attention_mask.cuda(self.device),
                decoder_start_token_id=self.decoder_start_token_id,
                num_beams=num_generate,
                num_return_sequences=num_generate,
                )

            dec = self.tokenizer.batch_decode(summaries, skip_special_tokens=True, clean_up_tokenization_spaces=False)
            decs.append(dec)

            return decs
