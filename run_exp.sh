log_name="k*k+dpp"
python evaluation.py --task fewrel --inductor rule --mlm_training True --bart_training True --group_beam True --log_name fewrel-${log_name} --device 0 & \
python evaluation.py --task nyt10 --inductor rule --mlm_training True --bart_training True --group_beam True --log_name nyt10-${log_name} --device 1 & \
python evaluation.py --task wiki80 --inductor rule --mlm_training True --bart_training True --group_beam True --log_name wiki80-${log_name} --device 7 & \
python evaluation.py --task TREx --inductor rule --mlm_training True --bart_training True --group_beam True --log_name TREx-${log_name} --device 8 & \
python evaluation.py --task google-re --inductor rule --mlm_training True --bart_training True --group_beam True --log_name google-${log_name} --device 5 & \
python evaluation.py --task semeval --inductor rule --mlm_training True --bart_training True --group_beam True --log_name semeval-${log_name} --device 6